import builtins
import json
from pathlib import Path

import pytest

from core import auth_parser, parser, profile_manager, remote_proxy, toml_parser, vscode_parser


IO_ERROR_KINDS = ("permission", "os-error", "sharing-violation")


def _make_io_error(kind: str) -> OSError:
    if kind == "permission":
        return PermissionError("configuration access denied")
    if kind == "sharing-violation":
        error = OSError("configuration is locked by another process")
        error.winerror = 32
        return error
    return OSError("configuration read failed")


@pytest.mark.parametrize("error_kind", IO_ERROR_KINDS)
def test_codex_toml_read_propagates_io_errors_and_preserves_file(
    monkeypatch,
    tmp_path,
    error_kind,
):
    config_path = tmp_path / "config.toml"
    original = b'model = "gpt-5.5"\n'
    config_path.write_bytes(original)
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", config_path)
    toml_parser.clear_codex_config_cache()

    real_open = builtins.open

    def failing_open(file, *args, **kwargs):
        if Path(file) == config_path:
            raise _make_io_error(error_kind)
        return real_open(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", failing_open)
        with pytest.raises(OSError) as exc_info:
            toml_parser.read_codex_config()

    if error_kind == "permission":
        assert isinstance(exc_info.value, PermissionError)
    if error_kind == "sharing-violation":
        assert getattr(exc_info.value, "winerror", None) == 32
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    ("module", "path_attr", "read_name", "clear_name"),
    (
        (parser, "CLAUDE_SETTINGS", "read_claude_settings", "clear_claude_file_cache"),
        (parser, "CLAUDE_CONFIG", "read_claude_config", "clear_claude_file_cache"),
        (parser, "CLAUDE_CREDENTIALS", "read_claude_credentials", "clear_claude_file_cache"),
        (auth_parser, "CODEX_AUTH", "read_codex_auth", "clear_codex_auth_cache"),
        (vscode_parser, "VSCODE_SETTINGS", "read_vscode_settings", "clear_vscode_settings_cache"),
    ),
)
@pytest.mark.parametrize("error_kind", IO_ERROR_KINDS)
def test_json_config_reads_propagate_io_errors_and_preserve_file(
    monkeypatch,
    tmp_path,
    module,
    path_attr,
    read_name,
    clear_name,
    error_kind,
):
    config_path = tmp_path / f"{path_attr.lower()}.json"
    original = b'{"sentinel":"keep"}'
    config_path.write_bytes(original)
    monkeypatch.setattr(module, path_attr, config_path)
    getattr(module, clear_name)()

    path_type = type(config_path)
    real_read_text = path_type.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == config_path:
            raise _make_io_error(error_kind)
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(path_type, "read_text", failing_read_text)
        with pytest.raises(OSError) as exc_info:
            getattr(module, read_name)()

    if error_kind == "permission":
        assert isinstance(exc_info.value, PermissionError)
    if error_kind == "sharing-violation":
        assert getattr(exc_info.value, "winerror", None) == 32
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    ("module", "path_attr", "read_name", "clear_name"),
    (
        (parser, "CLAUDE_SETTINGS", "read_claude_settings", "clear_claude_file_cache"),
        (parser, "CLAUDE_CONFIG", "read_claude_config", "clear_claude_file_cache"),
        (parser, "CLAUDE_CREDENTIALS", "read_claude_credentials", "clear_claude_file_cache"),
        (auth_parser, "CODEX_AUTH", "read_codex_auth", "clear_codex_auth_cache"),
        (vscode_parser, "VSCODE_SETTINGS", "read_vscode_settings", "clear_vscode_settings_cache"),
    ),
)
@pytest.mark.parametrize("content", ("{broken", "[]"))
def test_json_config_reads_reject_malformed_or_non_object_content(
    monkeypatch,
    tmp_path,
    module,
    path_attr,
    read_name,
    clear_name,
    content,
):
    config_path = tmp_path / f"{path_attr.lower()}.json"
    config_path.write_text(content, encoding="utf-8")
    original = config_path.read_bytes()
    monkeypatch.setattr(module, path_attr, config_path)
    getattr(module, clear_name)()

    with pytest.raises(ValueError):
        getattr(module, read_name)()

    assert config_path.read_bytes() == original


def test_codex_toml_read_rejects_malformed_content(monkeypatch, tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "unterminated', encoding="utf-8")
    original = config_path.read_bytes()
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", config_path)
    toml_parser.clear_codex_config_cache()

    with pytest.raises(ValueError):
        toml_parser.read_codex_config()

    assert config_path.read_bytes() == original


@pytest.mark.parametrize("error_kind", IO_ERROR_KINDS)
def test_profile_store_read_propagates_io_errors_without_using_backup_or_empty_cache(
    monkeypatch,
    tmp_path,
    error_kind,
):
    profiles_path = tmp_path / "profiles.json"
    backup_path = profiles_path.with_suffix(".backup")
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_path)
    profile_manager.clear_profile_store_cache()

    old_store = profile_manager._get_default_store()
    old_store["sentinel"] = "cached"
    profiles_path.write_text(json.dumps(old_store), encoding="utf-8")
    assert profile_manager._load_store()["sentinel"] == "cached"

    new_store = profile_manager._get_default_store()
    new_store["sentinel"] = "new-data-with-a-different-size"
    new_bytes = json.dumps(new_store).encode("utf-8")
    profiles_path.write_bytes(new_bytes)
    backup_path.write_text(json.dumps({"sentinel": "backup"}), encoding="utf-8")
    backup_bytes = backup_path.read_bytes()

    path_type = type(profiles_path)
    real_read_text = path_type.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == profiles_path:
            raise _make_io_error(error_kind)
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(path_type, "read_text", failing_read_text)
        with pytest.raises(OSError):
            profile_manager._load_store()

    assert profile_manager._STORE_CACHE["sentinel"] == "cached"
    assert profiles_path.read_bytes() == new_bytes
    assert backup_path.read_bytes() == backup_bytes


@pytest.mark.parametrize("error_kind", IO_ERROR_KINDS)
def test_proxy_state_read_propagates_io_errors_and_preserves_cached_state(
    monkeypatch,
    tmp_path,
    error_kind,
):
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path)
    state_path = tmp_path / "proxy_subscriptions" / "subscription_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"url": "https://old.example/sub"}), encoding="utf-8")
    remote_proxy.clear_proxy_subscription_state_cache()
    assert remote_proxy.load_proxy_subscription_state()["url"] == "https://old.example/sub"

    new_bytes = json.dumps(
        {"url": "https://new.example/sub", "node_count": 12345},
    ).encode("utf-8")
    state_path.write_bytes(new_bytes)
    path_type = type(state_path)
    real_read_text = path_type.read_text

    def failing_read_text(self, *args, **kwargs):
        if self == state_path:
            raise _make_io_error(error_kind)
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(path_type, "read_text", failing_read_text)
        with pytest.raises(OSError):
            remote_proxy.load_proxy_subscription_state()

    assert remote_proxy._PROXY_SUBSCRIPTION_STATE_CACHE["url"] == "https://old.example/sub"
    assert state_path.read_bytes() == new_bytes


@pytest.mark.parametrize("error_kind", IO_ERROR_KINDS)
def test_cached_proxy_yaml_read_propagates_io_errors(monkeypatch, tmp_path, error_kind):
    cache_path = tmp_path / "subscription.yaml"
    original = b"proxies:\n  - {name: US, type: socks5, server: us.example, port: 1080}\n"
    cache_path.write_bytes(original)
    remote_proxy.clear_proxy_subscription_state_cache()
    path_type = type(cache_path)
    real_read_bytes = path_type.read_bytes

    def failing_read_bytes(self):
        if self == cache_path:
            raise _make_io_error(error_kind)
        return real_read_bytes(self)

    with monkeypatch.context() as patch:
        patch.setattr(path_type, "read_bytes", failing_read_bytes)
        with pytest.raises(OSError):
            remote_proxy.load_cached_proxy_subscription(
                {"saved_path": str(cache_path), "charset": "utf-8"},
            )

    assert cache_path.read_bytes() == original


def test_missing_config_files_still_return_empty_values(monkeypatch, tmp_path):
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", tmp_path / "missing.toml")
    monkeypatch.setattr(parser, "CLAUDE_SETTINGS", tmp_path / "missing-settings.json")
    monkeypatch.setattr(parser, "CLAUDE_CONFIG", tmp_path / "missing-config.json")
    monkeypatch.setattr(parser, "CLAUDE_CREDENTIALS", tmp_path / "missing-credentials.json")
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", tmp_path / "missing-auth.json")
    monkeypatch.setattr(vscode_parser, "VSCODE_SETTINGS", tmp_path / "missing-vscode.json")
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "missing.json")
    monkeypatch.setattr(remote_proxy, "STORAGE_DIR", tmp_path / "proxy")
    toml_parser.clear_codex_config_cache()
    parser.clear_claude_file_cache()
    auth_parser.clear_codex_auth_cache()
    vscode_parser.clear_vscode_settings_cache()
    profile_manager.clear_profile_store_cache()
    remote_proxy.clear_proxy_subscription_state_cache()

    assert toml_parser.read_codex_config() == {}
    assert parser.read_claude_settings() == {}
    assert parser.read_claude_config() == {}
    assert parser.read_claude_credentials() == {}
    assert auth_parser.read_codex_auth() == {}
    assert vscode_parser.read_vscode_settings() == {}
    assert profile_manager._load_store() == profile_manager._get_default_store()
    assert remote_proxy.load_proxy_subscription_state() == {}
    assert remote_proxy.load_cached_proxy_subscription(
        {"saved_path": str(tmp_path / "missing.yaml")},
    ) is None


def test_profile_store_migration_write_failure_is_not_reported_as_empty(
    monkeypatch,
    tmp_path,
):
    profiles_path = tmp_path / "profiles.json"
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_path)
    profile_manager.clear_profile_store_cache()
    old_store = profile_manager._get_default_store()
    old_store["version"] = 1
    original = json.dumps(old_store).encode("utf-8")
    profiles_path.write_bytes(original)

    def fail_save(*_args, **_kwargs):
        raise RuntimeError("Failed to save profiles: sharing violation") from OSError(
            "sharing violation",
        )

    monkeypatch.setattr(profile_manager, "_save_store", fail_save)

    with pytest.raises(RuntimeError, match="sharing violation"):
        profile_manager._load_store()

    assert profiles_path.read_bytes() == original


def test_locked_profile_backup_does_not_fall_back_to_empty_store(monkeypatch, tmp_path):
    profiles_path = tmp_path / "profiles.json"
    backup_path = profiles_path.with_suffix(".backup")
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", profiles_path)
    profile_manager.clear_profile_store_cache()
    profiles_path.write_text("{broken", encoding="utf-8")
    backup_path.write_text(json.dumps(profile_manager._get_default_store()), encoding="utf-8")
    original_primary = profiles_path.read_bytes()
    original_backup = backup_path.read_bytes()
    path_type = type(backup_path)
    real_read_text = path_type.read_text

    def locked_backup(self, *args, **kwargs):
        if self == backup_path:
            error = OSError("backup sharing violation")
            error.winerror = 32
            raise error
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(path_type, "read_text", locked_backup)

    with pytest.raises(OSError, match="backup sharing violation"):
        profile_manager._load_store()

    assert profiles_path.read_bytes() == original_primary
    assert backup_path.read_bytes() == original_backup
