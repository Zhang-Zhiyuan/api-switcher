import hashlib
import json
import importlib
import os
from copy import deepcopy
from io import BytesIO, StringIO
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from core import codex_env, persistent_env, profile_manager, remote_auto_continue, remote_config, remote_git_login, remote_proxy, security, sync_manager
from core.ssh_manager import SSHManager, ssh_manager
from core.ssh_profile_builder import build_ssh_profile_from_data, prepare_ssh_profile_from_data
from models.profile import ClaudeAccountProfile, ClaudeProfile, CodexAccountProfile, CodexProfile, SSHProfile
from ui.tabs.ssh_tab import SSHTab, _format_server_batch_item


class _ExecFailureClient:
    def exec_command(self, *_args, **_kwargs):
        raise EOFError()


def test_ssh_command_error_keeps_empty_transport_exception_actionable():
    manager = SSHManager()

    with pytest.raises(RuntimeError, match=r"执行远程命令失败: EOFError"):
        manager.execute_command_with_status(_ExecFailureClient(), "true")


class _RemoteSecretStore(dict):
    def __init__(self):
        super().__init__()
        self.remote_files: dict[str, str] = {}


@pytest.fixture()
def isolated_ssh(tmp_path, monkeypatch):
    secret_store = _RemoteSecretStore()

    monkeypatch.setattr(security, "set_secret", lambda key, value: secret_store.__setitem__(key, value or ""))
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "get_secret_strict", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, data: secret_store.__setitem__(key, json.dumps(data)))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secret_store[key]) if key in secret_store else None)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")

    # Remote sync now snapshots and verifies the exact destination files.  A
    # stable in-memory SSH filesystem keeps older focused tests concise while
    # exercising those transaction reads instead of bypassing them.
    remote_files = secret_store.remote_files
    monkeypatch.setattr(remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(
        remote_config,
        "read_remote_text",
        lambda _client, path: remote_files.get(path),
    )
    monkeypatch.setattr(
        remote_config,
        "write_remote_text",
        lambda _client, path, content, file_mode=None: remote_files.__setitem__(path, content),
    )
    monkeypatch.setattr(
        remote_config,
        "delete_remote_file",
        lambda _client, path: remote_files.pop(path, None),
    )

    return secret_store


def _install_codex_sync_state(
    monkeypatch,
    isolated_ssh,
    *,
    config=None,
    auth=None,
    dotenv="",
    persistent="",
):
    from core import codex_env

    state = {
        "config": deepcopy(config or {}),
        "auth": deepcopy(auth or {}),
        "dotenv": dotenv,
    }
    writes = {"config": [], "auth": [], "dotenv": [], "persistent": [], "deleted": []}
    files = isolated_ssh.remote_files
    config_path = "~/.codex/config.toml"
    auth_path = "~/.codex/auth.json"
    dotenv_path = "~/.codex/.env"
    persistent_path = "/home/test/.api_switcher_env"
    import tomli_w

    files[config_path] = tomli_w.dumps(state["config"])
    files[auth_path] = json.dumps(state["auth"])
    if dotenv is not None:
        files[dotenv_path] = dotenv
    files[persistent_path] = persistent

    def read_config(_client, _profile=None, **_kwargs):
        return deepcopy(state["config"])

    def write_config(client, data, profile=None):
        state["config"] = deepcopy(data)
        files[config_path] = tomli_w.dumps(data)
        writes["config"].append((client, deepcopy(data), profile))

    def read_auth(_client, _profile=None, **_kwargs):
        return deepcopy(state["auth"])

    def write_auth(client, data, profile=None):
        state["auth"] = deepcopy(data)
        files[auth_path] = json.dumps(data)
        writes["auth"].append((client, deepcopy(data), profile))

    def read_dotenv(_client, _profile=None):
        return state["dotenv"]

    def write_dotenv(_client, content, _profile=None):
        state["dotenv"] = content
        files[dotenv_path] = content
        writes["dotenv"].append(content)

    def set_persistent(client, updates):
        files[persistent_path] = codex_env.merge_codex_env_text(files.get(persistent_path, ""), updates=updates)
        writes["persistent"].append((client, dict(updates)))

    def delete_persistent(client, names):
        names = list(names)
        files[persistent_path] = codex_env.merge_codex_env_text(files.get(persistent_path, ""), deletes=names)
        writes["deleted"].append((client, names))

    monkeypatch.setattr(remote_config, "read_remote_codex_config", read_config)
    monkeypatch.setattr(remote_config, "write_remote_codex_config", write_config)
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", read_auth)
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", write_auth)
    monkeypatch.setattr(remote_config, "read_remote_codex_env", read_dotenv)
    monkeypatch.setattr(remote_config, "write_remote_codex_env", write_dotenv)
    monkeypatch.setattr(persistent_env, "set_remote_user_env", set_persistent)
    monkeypatch.setattr(persistent_env, "delete_remote_user_env", delete_persistent)
    return state, writes


def _install_claude_sync_state(
    monkeypatch,
    isolated_ssh,
    *,
    settings=None,
    config=None,
    credentials=None,
    vscode=None,
):
    state = {
        "settings": deepcopy(settings or {}),
        "config": deepcopy(config or {}),
        "credentials": deepcopy(credentials) if credentials is not None else None,
        "vscode": deepcopy(vscode or {}),
    }
    writes = {"settings": [], "config": [], "credentials": [], "vscode": [], "persistent_deleted": []}
    files = isolated_ssh.remote_files
    persistent_path = "/home/test/.api_switcher_env"
    paths = {
        "settings": "~/.claude/settings.json",
        "config": "~/.claude/config.json",
        "credentials": "~/.claude/.credentials.json",
    }
    files[paths["settings"]] = json.dumps(state["settings"])
    files[paths["config"]] = json.dumps(state["config"])
    if credentials is not None:
        files[paths["credentials"]] = json.dumps(credentials)
    vscode_paths = [
        path.replace("~", "/home/test", 1)
        for path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS
    ]
    if vscode is not None:
        files[vscode_paths[0]] = json.dumps(state["vscode"])

    def reader(key):
        def read(_client, _profile=None, **_kwargs):
            return deepcopy(state[key])
        return read

    def writer(key):
        def write(client, data, profile=None):
            state[key] = deepcopy(data)
            files[paths[key]] = json.dumps(data)
            writes[key].append((client, deepcopy(data), profile))
        return write

    monkeypatch.setattr(remote_config, "read_remote_claude_settings", reader("settings"))
    monkeypatch.setattr(remote_config, "write_remote_claude_settings", writer("settings"))
    monkeypatch.setattr(remote_config, "read_remote_claude_config", reader("config"))
    monkeypatch.setattr(remote_config, "write_remote_claude_config", writer("config"))
    monkeypatch.setattr(remote_config, "read_remote_claude_credentials", reader("credentials"))
    monkeypatch.setattr(remote_config, "write_remote_claude_credentials", writer("credentials"))

    def read_vscode_path(_client, path, *, strict=False):
        content = files.get(path)
        if content is None:
            return None
        try:
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("not an object")
            return deepcopy(value)
        except Exception as error:
            if strict:
                raise RuntimeError("strict JSON failure") from error
            return None

    def write_vscode_path(_client, path, data, file_mode=None):
        files[path] = json.dumps(data)
        if path == vscode_paths[0]:
            state["vscode"] = deepcopy(data)
        writes["vscode"].append(deepcopy(data))

    monkeypatch.setattr(remote_config, "read_remote_json", read_vscode_path)
    monkeypatch.setattr(remote_config, "write_remote_json", write_vscode_path)

    def delete_persistent(client, names):
        names = list(names)
        current = files.get(persistent_path)
        if current is not None:
            files[persistent_path] = persistent_env._remove_env_exports(current, names)
        writes["persistent_deleted"].append((client, names))

    monkeypatch.setattr(persistent_env, "delete_remote_user_env", delete_persistent)
    return state, writes


def test_proxy_latency_stops_when_no_server_is_selected():
    class FakeTab:
        _proxy_busy = False
        _proxy_subscription_nodes = [object()]

        def __init__(self):
            self.required_selection = False

        def _require_selected_servers(self, _status_setter):
            self.required_selection = True
            return []

        def _set_proxy_status(self, *_args, **_kwargs):
            raise AssertionError("status should be set by _require_selected_servers")

        def _format_server_target(self, *_args, **_kwargs):
            raise AssertionError("must not format an empty target")

        def _run_proxy_ssh_task(self, *_args, **_kwargs):
            raise AssertionError("must not start latency task without a server")

    fake = FakeTab()

    SSHTab._measure_proxy_subscription_latencies(fake)

    assert fake.required_selection is True


def test_ssh_builder_preserves_password_when_editing_metadata(isolated_ssh):
    security.set_secret("ssh:prod:password", "secret-password")
    existing = SSHProfile(
        name="prod",
        host="old.example.com",
        port=22,
        username="root",
        auth_type="password",
        password_ref="ssh:prod:password",
    )

    profile = build_ssh_profile_from_data(
        {
            "name": "prod",
            "host": "new.example.com",
            "port": "2200",
            "username": "admin",
            "auth_type": "password",
            "password": "",
            "private_key_path": "",
            "key_passphrase": "",
        },
        existing,
    )

    assert profile.password_ref == "ssh:prod:password"
    assert profile.host == "new.example.com"
    assert profile.port == 2200
    assert security.get_secret(profile.password_ref) == "secret-password"


def test_ssh_save_plan_defers_secret_write_until_transaction(isolated_ssh):
    secret_store = isolated_ssh
    plan = prepare_ssh_profile_from_data({
        "name": "deferred",
        "host": "ssh.example.com",
        "port": "22",
        "username": "root",
        "auth_type": "password",
        "password": "new-password",
        "private_key_path": "",
        "key_passphrase": "",
    })
    ref = "ssh:deferred:password"

    assert plan.profile.password_ref == ref
    assert plan.secret_updates == {ref: "new-password"}
    assert ref not in secret_store

    profile_manager.save_ssh_profile_with_secrets(plan.profile, plan.secret_updates)

    assert secret_store[ref] == "new-password"
    assert [profile.name for profile in profile_manager.list_ssh_profiles()] == ["deferred"]


def test_format_server_batch_item_avoids_duplicate_server_prefix():
    assert _format_server_batch_item("4090", "4090: AI 代理已清理") == "4090: AI 代理已清理"
    assert _format_server_batch_item("4090", "已同步配置") == "4090: 已同步配置"


def test_win11_proxy_tab_is_registered_and_importable():
    app_module = importlib.import_module("ui.app")
    specs = {label: spec for label, *spec in app_module.TAB_SPECS}

    assert "Win11 代理" in specs
    _attr, module_name, class_name, _eager = specs["Win11 代理"]
    tab_module = importlib.import_module(module_name)

    assert hasattr(tab_module, class_name)


def test_ssh_rename_copies_secret_and_removes_old_ref(isolated_ssh, monkeypatch):
    disconnected = []
    monkeypatch.setattr(ssh_manager, "disconnect", lambda name: disconnected.append(name))

    security.set_secret("ssh:prod:password", "secret-password")
    old = SSHProfile(
        name="prod",
        host="old.example.com",
        auth_type="password",
        password_ref="ssh:prod:password",
    )
    profile_manager.save_ssh_profile(old)
    profile_manager.set_active_ssh("prod")

    renamed = build_ssh_profile_from_data(
        {
            "name": "prod-renamed",
            "host": "new.example.com",
            "port": "22",
            "username": "root",
            "auth_type": "password",
            "password": "",
            "private_key_path": "",
            "key_passphrase": "",
        },
        old,
    )
    profile_manager.save_ssh_profile(renamed, previous_name=old.name)

    profiles = profile_manager.list_ssh_profiles()
    assert [profile.name for profile in profiles] == ["prod-renamed"]
    assert profile_manager.get_active_ssh_name() == "prod-renamed"
    assert renamed.password_ref == "ssh:prod-renamed:password"
    assert security.get_secret("ssh:prod-renamed:password") == "secret-password"
    assert security.get_secret("ssh:prod:password") is None
    assert {"prod", "prod-renamed"}.issubset(set(disconnected))


def test_ssh_switching_from_password_to_key_prunes_password_secret(isolated_ssh):
    security.set_secret("ssh:prod:password", "secret-password")
    old = SSHProfile(
        name="prod",
        host="server.example.com",
        auth_type="password",
        password_ref="ssh:prod:password",
    )
    profile_manager.save_ssh_profile(old)

    key_profile = build_ssh_profile_from_data(
        {
            "name": "prod",
            "host": "server.example.com",
            "port": "22",
            "username": "root",
            "auth_type": "key",
            "password": "",
            "private_key_path": "/home/root/.ssh/id_ed25519",
            "key_passphrase": "",
        },
        old,
    )
    profile_manager.save_ssh_profile(key_profile, previous_name=old.name)

    [saved] = profile_manager.list_ssh_profiles()
    assert saved.auth_type == "key"
    assert saved.password_ref is None
    assert saved.private_key_path == "/home/root/.ssh/id_ed25519"
    assert security.get_secret("ssh:prod:password") is None


def test_ssh_builder_accepts_custom_remote_config_dirs(isolated_ssh):
    profile = build_ssh_profile_from_data(
        {
            "name": "prod",
            "host": "server.example.com",
            "port": "22",
            "username": "root",
            "auth_type": "key",
            "password": "",
            "private_key_path": "/home/root/.ssh/id_ed25519",
            "key_passphrase": "",
            "remote_claude_dir": "$HOME/.config/claude",
            "remote_codex_dir": "/srv/codex\\state/",
        }
    )

    assert profile.remote_claude_dir == "$HOME/.config/claude"
    assert profile.remote_codex_dir == "/srv/codex/state"


def test_sync_codex_to_server_uses_ssh_manager_instance(isolated_ssh, monkeypatch):
    security.set_secret("codex:relay:api_key", "sk-relay")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="relay",
            api_key_ref="codex:relay:api_key",
            model="relay-model",
            model_provider="custom",
            custom_base_url="https://relay.example.com/v1",
        )
    )

    connected = {}
    fake_client = object()

    def fake_connect(profile):
        connected["profile"] = profile
        return fake_client

    monkeypatch.setattr(sync_manager.ssh_manager, "connect", fake_connect)
    _state, writes = _install_codex_sync_state(
        monkeypatch,
        isolated_ssh,
        auth={"auth_mode": "chatgpt", "tokens": {"id": "old"}},
    )

    message = sync_manager.sync_codex_to_server("remote", "relay")

    assert connected["profile"].name == "remote"
    assert writes["config"][-1][0] is fake_client
    assert writes["auth"][-1][1]["auth_mode"] == "chatgpt"
    assert "OPENAI_API_KEY" not in writes["auth"][-1][1]
    assert writes["auth"][-1][2].name == "remote"
    assert writes["persistent"] == [(fake_client, {"OPENAI_API_KEY": "sk-relay"})]
    assert "OPENAI_API_KEY" in message
    assert "ssh.example.com" in message


def test_sync_codex_to_server_writes_provider_env_key(isolated_ssh, monkeypatch):
    security.set_secret("codex:deepseek:api_key", "sk-deepseek")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="deepseek",
            api_key_ref="codex:deepseek:api_key",
            model="deepseek-v4-flash",
            model_provider="deepseek",
        )
    )

    fake_client = object()

    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_codex_sync_state(monkeypatch, isolated_ssh)

    message = sync_manager.sync_codex_to_server("remote", "deepseek")

    assert writes["persistent"][-1][1] == {"DEEPSEEK_API_KEY": "sk-deepseek"}
    assert "DEEPSEEK_API_KEY" in message
    assert "OPENAI_API_KEY" not in message


def test_sync_codex_to_server_applies_remote_wire_api_benchmark(isolated_ssh, monkeypatch):
    security.set_secret("codex:deepseek:api_key", "sk-deepseek")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="deepseek",
            api_key_ref="codex:deepseek:api_key",
            model="deepseek-v4-flash",
            model_provider="deepseek",
            custom_base_url="https://api.deepseek.com",
            custom_wire_api="responses",
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_codex_sync_state(monkeypatch, isolated_ssh)
    monkeypatch.setattr(
        sync_manager,
        "_remote_benchmark_codex_wire_api",
        lambda client, profile, config, api_key: sync_manager.RemoteWireBenchmarkResult(
            True,
            recommended_wire_api="responses",
            selected_model="gpt-5.5",
            summary="responses 3/3 avg 1000ms",
        ),
    )

    message = sync_manager.sync_codex_to_server("remote", "deepseek")

    assert len(writes["config"]) == 1
    assert writes["config"][0][1]["model_providers"]["deepseek"]["wire_api"] == "responses"
    assert "wire_api=responses" in message
    assert "responses 3/3" in message


def test_sync_codex_to_server_can_force_wire_api_without_benchmark(isolated_ssh, monkeypatch):
    security.set_secret("codex:deepseek:api_key", "sk-deepseek")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="deepseek",
            api_key_ref="codex:deepseek:api_key",
            model="deepseek-v4-flash",
            model_provider="deepseek",
            custom_base_url="https://api.deepseek.com",
            custom_wire_api="responses",
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_codex_sync_state(monkeypatch, isolated_ssh)
    monkeypatch.setattr(
        sync_manager,
        "_remote_benchmark_codex_wire_api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("manual wire_api must not benchmark")),
    )

    message = sync_manager.sync_codex_to_server("remote", "deepseek", wire_api_mode="chat")

    assert len(writes["config"]) == 1
    assert writes["config"][0][1]["model_providers"]["deepseek"]["wire_api"] == "responses"
    assert "wire_api=responses" in message


def test_sync_codex_to_server_profile_mode_uses_effective_local_wire_api(isolated_ssh, monkeypatch):
    security.set_secret("codex:deepseek:api_key", "sk-deepseek")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="deepseek",
            api_key_ref="codex:deepseek:api_key",
            model="deepseek-v4-flash",
            model_provider="deepseek",
            custom_base_url="https://api.deepseek.com",
            custom_wire_api=None,
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_codex_sync_state(monkeypatch, isolated_ssh)
    monkeypatch.setattr(
        sync_manager,
        "_remote_benchmark_codex_wire_api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("profile mode must not benchmark")),
    )

    message = sync_manager.sync_codex_to_server("remote", "deepseek", wire_api_mode="profile")

    assert len(writes["config"]) == 1
    assert writes["config"][0][1]["model_providers"]["deepseek"]["wire_api"] == "responses"
    assert "wire_api=responses" in message


def test_remote_codex_wire_api_benchmark_handles_empty_output(monkeypatch):
    profile = CodexProfile(
        name="openai",
        model="gpt-5.5",
        model_provider="openai",
        custom_base_url="https://openai.example.com/v1",
    )

    monkeypatch.setattr(sync_manager, "_remote_codex_base_url", lambda config, p: "https://openai.example.com/v1")
    monkeypatch.setattr(sync_manager, "_remote_codex_model", lambda config, p: "gpt-5.5")
    monkeypatch.setattr(remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(sync_manager.ssh_manager, "read_remote_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sync_manager.ssh_manager,
        "execute_command_with_status",
        lambda *args, **kwargs: (0, "", ""),
    )

    result = sync_manager._remote_benchmark_codex_wire_api(object(), profile, {}, "sk-test")

    assert result.success is False
    assert result.error == "远端 wire_api 自测没有输出"


def test_remote_codex_wire_api_benchmark_uses_remote_error(monkeypatch):
    profile = CodexProfile(
        name="openai",
        model="gpt-5.5",
        model_provider="openai",
        custom_base_url="https://openai.example.com/v1",
    )

    monkeypatch.setattr(sync_manager, "_remote_codex_base_url", lambda config, p: "https://openai.example.com/v1")
    monkeypatch.setattr(sync_manager, "_remote_codex_model", lambda config, p: "gpt-5.5")
    monkeypatch.setattr(remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(sync_manager.ssh_manager, "read_remote_file", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        sync_manager.ssh_manager,
        "execute_command_with_status",
        lambda *args, **kwargs: (0, '{"success": false, "error": "invalid payload"}\n', ""),
    )

    result = sync_manager._remote_benchmark_codex_wire_api(object(), profile, {}, "sk-test")

    assert result.success is False
    assert result.error == "invalid payload"


def test_remote_codex_wire_benchmark_rejects_drifted_strict_config_before_key_network(
    monkeypatch,
):
    profile = CodexProfile(
        name="openai",
        model="gpt-5.5",
        model_provider="openai",
        custom_base_url="https://openai.example.com/v1",
    )
    strict_config = remote_proxy.build_mihomo_config(
        {
            "name": "node",
            "type": "vless",
            "server": "node.example.com",
            "port": 443,
            "uuid": "11111111-1111-1111-1111-111111111111",
        },
        strict_privacy=True,
    )
    drifted_config = strict_config.replace("MATCH,AI-PROXY", "MATCH,DIRECT", 1)
    assert remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER in drifted_config
    assert remote_proxy._managed_config_strict_privacy_enabled(drifted_config) is False

    monkeypatch.setattr(sync_manager, "_remote_codex_base_url", lambda _config, _profile: "https://openai.example.com/v1")
    monkeypatch.setattr(sync_manager, "_remote_codex_model", lambda _config, _profile: "gpt-5.5")
    monkeypatch.setattr(remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(
        sync_manager.ssh_manager,
        "read_remote_file",
        lambda *_args, **_kwargs: drifted_config,
    )

    def must_not_execute(*_args, **_kwargs):
        raise AssertionError("drifted strict route must fail before sending API key")

    monkeypatch.setattr(
        sync_manager.ssh_manager,
        "execute_command_with_status",
        must_not_execute,
    )

    result = sync_manager._remote_benchmark_codex_wire_api(
        object(),
        profile,
        {},
        "sk-real-secret-must-not-be-sent",
    )

    assert result.success is False
    assert "完整契约校验" in result.error


def test_remote_codex_wire_benchmark_valid_strict_contract_uses_validated_proxy_token(
    monkeypatch,
):
    profile = CodexProfile(
        name="openai",
        model="gpt-5.5",
        model_provider="openai",
        custom_base_url="https://openai.example.com/v1",
    )
    strict_config = remote_proxy.build_mihomo_config(
        {
            "name": "node",
            "type": "vless",
            "server": "node.example.com",
            "port": 443,
            "uuid": "11111111-1111-1111-1111-111111111111",
        },
        strict_privacy=True,
    )
    captured = {}
    monkeypatch.setattr(sync_manager, "_remote_codex_base_url", lambda _config, _profile: "https://openai.example.com/v1")
    monkeypatch.setattr(sync_manager, "_remote_codex_model", lambda _config, _profile: "gpt-5.5")
    monkeypatch.setattr(remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(
        sync_manager.ssh_manager,
        "read_remote_file",
        lambda *_args, **_kwargs: strict_config,
    )

    def execute(_client, command, **kwargs):
        captured["command"] = command
        captured["input_data"] = kwargs["input_data"]
        return (
            0,
            json.dumps(
                {
                    "success": True,
                    "recommended_wire_api": "responses",
                    "selected_model": "gpt-5.5",
                    "summaries": [],
                }
            ),
            "",
        )

    monkeypatch.setattr(sync_manager.ssh_manager, "execute_command_with_status", execute)

    result = sync_manager._remote_benchmark_codex_wire_api(
        object(),
        profile,
        {},
        "sk-real-secret",
    )

    assert result.success is True
    fingerprint = hashlib.sha256(strict_config.encode("utf-8")).hexdigest()
    assert f"strict-sha256:{fingerprint}" in captured["command"]
    assert "sk-real-secret" not in captured["command"]
    assert json.loads(captured["input_data"])["api_key"] == "sk-real-secret"


def _run_remote_wire_benchmark_script(
    monkeypatch,
    capsys,
    env_path,
    opener,
    *,
    config_path="",
    validation_token="",
    repeat_count=1,
):
    built_proxies = []

    def fake_build_opener(*handlers):
        assert len(handlers) == 1
        built_proxies.append(dict(handlers[0].proxies))
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", fake_build_opener)
    monkeypatch.setattr(
        sys,
        "stdin",
        StringIO(
            json.dumps(
                {
                    "api_key": "sk-offline-test",
                    "base_url": "https://api.example.test/v1",
                    "model": "offline-model",
                    "timeout": 1,
                    "repeat_count": repeat_count,
                    "wire_apis": ["responses"],
                }
            )
        ),
    )
    effective_config_path = (
        Path(config_path)
        if config_path
        else Path(env_path).with_name("missing-mihomo-config.yaml")
    )
    if not validation_token:
        if effective_config_path.exists():
            config_text = effective_config_path.read_text(
                encoding="utf-8-sig",
                errors="replace",
            )
            lines = {line.strip() for line in config_text.splitlines()}
            mode = (
                "strict"
                if remote_proxy.AI_PROXY_CONFIG_MARKER in lines
                and remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER in lines
                else "compat"
            )
            validation_token = (
                f"{mode}-sha256:"
                f"{hashlib.sha256(config_text.encode('utf-8')).hexdigest()}"
            )
        else:
            validation_token = "absent"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "remote-wire-benchmark",
            str(env_path),
            str(effective_config_path),
            validation_token,
        ],
    )
    exec(sync_manager._REMOTE_CODEX_WIRE_BENCHMARK_SCRIPT, {})
    output = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert output
    return json.loads(output[-1]), built_proxies


def test_remote_wire_benchmark_ignores_no_proxy_and_uses_managed_loopback(
    monkeypatch,
    capsys,
    tmp_path,
):
    env_path = tmp_path / "ai-proxy.env"
    env_path.write_text(
        "# Managed by API切换器. Non-AI domains are DIRECT in mihomo rules.\n"
        "export API_SWITCHER_AI_PROXY_URL=http://127.0.0.1:7890\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        remote_proxy.build_mihomo_config(
            {
                "name": "node",
                "type": "vless",
                "server": "node.example.com",
                "port": 443,
                "uuid": "11111111-1111-1111-1111-111111111111",
            },
            strict_privacy=True,
        ),
        encoding="utf-8",
    )
    observations = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            if observations and observations[-1].get("read"):
                return b""
            observations[-1]["read"] = True
            return b"event: response.completed\n"

    class Opener:
        def open(self, request, timeout):
            observations.append(
                {
                    "url": request.full_url,
                    "timeout": timeout,
                    "NO_PROXY": os.environ.get("NO_PROXY"),
                    "no_proxy": os.environ.get("no_proxy"),
                }
            )
            return Response()

    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    result, built_proxies = _run_remote_wire_benchmark_script(
        monkeypatch,
        capsys,
        env_path,
        Opener(),
        config_path=config_path,
    )

    assert result["success"] is True
    assert built_proxies == [
        {
            "http": "http://127.0.0.1:7890",
            "https": "http://127.0.0.1:7890",
        }
    ]
    assert len(observations) == 1
    assert observations[0]["url"] == "https://api.example.test/v1/responses"
    assert observations[0]["NO_PROXY"] is None
    assert observations[0]["no_proxy"] is None


def test_remote_wire_benchmark_proxy_failure_has_no_direct_retry(
    monkeypatch,
    capsys,
    tmp_path,
):
    env_path = tmp_path / "ai-proxy.env"
    env_path.write_text(
        "# Managed by API切换器. Non-AI domains are DIRECT in mihomo rules.\n"
        "export API_SWITCHER_AI_PROXY_URL=http://127.0.0.1:17890\n",
        encoding="utf-8",
    )
    opened = []

    class UnavailableProxyOpener:
        def open(self, request, timeout):
            opened.append((request.full_url, timeout))
            raise urllib.error.URLError("loopback proxy unavailable")

    monkeypatch.setenv("NO_PROXY", "*")
    result, built_proxies = _run_remote_wire_benchmark_script(
        monkeypatch,
        capsys,
        env_path,
        UnavailableProxyOpener(),
    )

    assert result["success"] is False
    assert result["summaries"][0]["successes"] == 0
    assert "loopback proxy unavailable" in result["summaries"][0]["error"]
    assert built_proxies == [
        {
            "http": "http://127.0.0.1:17890",
            "https": "http://127.0.0.1:17890",
        }
    ]
    assert opened == [("https://api.example.test/v1/responses", 1)]


@pytest.mark.parametrize("env_state", ["missing", "damaged"])
def test_remote_wire_benchmark_strict_config_never_falls_back_to_direct(
    monkeypatch,
    capsys,
    tmp_path,
    env_state,
):
    env_path = tmp_path / "ai-proxy.env"
    if env_state == "damaged":
        env_path.write_text(
            "# Managed by API切换器. Non-AI domains are DIRECT in mihomo rules.\n"
            "export API_SWITCHER_AI_PROXY_URL=not-a-loopback-proxy\n",
            encoding="utf-8",
        )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        remote_proxy.build_mihomo_config(
            {
                "name": "node",
                "type": "vless",
                "server": "node.example.com",
                "port": 443,
                "uuid": "11111111-1111-1111-1111-111111111111",
            },
            strict_privacy=True,
        ),
        encoding="utf-8",
    )
    opened = []

    class MustNotOpen:
        def open(self, request, timeout):
            opened.append((request.full_url, timeout))
            raise AssertionError("strict configuration must fail before network access")

    result, built_proxies = _run_remote_wire_benchmark_script(
        monkeypatch,
        capsys,
        env_path,
        MustNotOpen(),
        config_path=config_path,
    )

    assert result["success"] is False
    assert "managed proxy configuration invalid" in result["error"]
    assert built_proxies == []
    assert opened == []


@pytest.mark.parametrize("config_state", ["missing", "compat"])
def test_remote_wire_benchmark_without_strict_config_preserves_direct_compatibility(
    monkeypatch,
    capsys,
    tmp_path,
    config_state,
):
    env_path = tmp_path / "missing-ai-proxy.env"
    config_path = tmp_path / "config.yaml"
    if config_state == "compat":
        config_path.write_text(
            "# Managed by API切换器 AI proxy\n"
            "mixed-port: 7890\n"
            "rules:\n  - MATCH,DIRECT\n",
            encoding="utf-8",
        )
    opened = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def readline(self):
            if opened[-1].get("read"):
                return b""
            opened[-1]["read"] = True
            return b"event: response.completed\n"

    class DirectOpener:
        def open(self, request, timeout):
            opened.append({"url": request.full_url, "timeout": timeout})
            return Response()

    result, built_proxies = _run_remote_wire_benchmark_script(
        monkeypatch,
        capsys,
        env_path,
        DirectOpener(),
        config_path=config_path,
    )

    assert result["success"] is True
    assert built_proxies == [{}]
    assert [item["url"] for item in opened] == [
        "https://api.example.test/v1/responses"
    ]


def test_remote_wire_benchmark_unreadable_config_fails_before_network(
    monkeypatch,
    capsys,
    tmp_path,
):
    env_path = tmp_path / "missing-ai-proxy.env"
    config_path = tmp_path / "config.yaml"
    config_path.mkdir()

    class MustNotOpen:
        def open(self, request, timeout):
            raise AssertionError("unreadable config must fail before network access")

    result, built_proxies = _run_remote_wire_benchmark_script(
        monkeypatch,
        capsys,
        env_path,
        MustNotOpen(),
        config_path=config_path,
        validation_token="strict-sha256:" + "0" * 64,
    )

    assert result["success"] is False
    assert "cannot read validated managed proxy config" in result["error"]
    assert built_proxies == []


def test_sync_claude_to_root_downgrades_bypass_permissions(isolated_ssh, monkeypatch):
    security.set_secret("claude:relay:auth_token", "sk-relay")
    ssh_profile = SSHProfile(name="remote", host="ssh.example.com", username="root")
    profile_manager.save_ssh_profile(ssh_profile)
    profile_manager.save_claude_profile(
        ClaudeProfile(
            name="relay",
            auth_token_ref="claude:relay:auth_token",
            base_url="https://relay.example.com/anthropic",
            provider="deepseek",
            permissions_mode="bypassPermissions",
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_claude_sync_state(
        monkeypatch,
        isolated_ssh,
        vscode={
            "claudeCode.initialPermissionMode": "bypassPermissions",
            "claudeCode.allowDangerouslySkipPermissions": True,
        },
    )

    message = sync_manager.sync_claude_to_server("remote", "relay")

    assert writes["settings"][-1][1]["permissions"]["defaultMode"] == "dontAsk"
    assert writes["settings"][-1][1]["skipDangerousModePermissionPrompt"] is False
    assert writes["vscode"][-1]["claudeCode.initialPermissionMode"] == "dontAsk"
    assert writes["vscode"][-1]["claudeCode.allowDangerouslySkipPermissions"] is False
    assert "已兼容 root 登录" in message
    assert "root" in message
    assert "--dangerously-skip-permissions" in message


def test_sync_claude_to_non_root_preserves_bypass_permissions(isolated_ssh, monkeypatch):
    security.set_secret("claude:relay:auth_token", "sk-relay")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_claude_profile(
        ClaudeProfile(
            name="relay",
            auth_token_ref="claude:relay:auth_token",
            base_url="https://relay.example.com/anthropic",
            provider="deepseek",
            permissions_mode="bypassPermissions",
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_claude_sync_state(monkeypatch, isolated_ssh)
    monkeypatch.setattr(remote_config, "read_remote_vscode_settings", lambda client: (_ for _ in ()).throw(AssertionError("non-root should not read VS Code settings")))
    monkeypatch.setattr(remote_config, "write_remote_vscode_settings", lambda client, data: (_ for _ in ()).throw(AssertionError("non-root should not write VS Code settings")))

    message = sync_manager.sync_claude_to_server("remote", "relay")

    assert writes["settings"][-1][1]["permissions"]["defaultMode"] == "bypassPermissions"
    assert "已兼容 root 登录" not in message


def test_root_safety_forces_no_prompt_mode():
    profile = SSHProfile(name="remote", host="ssh.example.com", username="root")

    settings, changed = sync_manager._make_claude_settings_root_safe(
        {"permissions": {"defaultMode": "acceptEdits"}},
        profile,
    )
    assert changed is True
    assert settings["permissions"]["defaultMode"] == "dontAsk"

    missing_permissions, changed = sync_manager._make_claude_settings_root_safe({}, profile)
    assert changed is True
    assert missing_permissions["permissions"]["defaultMode"] == "dontAsk"
    assert missing_permissions["skipDangerousModePermissionPrompt"] is False

    vscode, changed = sync_manager._make_vscode_settings_root_safe(
        {
            "claudeCode.initialPermissionMode": "acceptEdits",
            "claudeCode.allowDangerouslySkipPermissions": False,
        },
        profile,
    )
    assert changed is True
    assert vscode["claudeCode.initialPermissionMode"] == "dontAsk"


def test_sync_claude_account_to_server_writes_credentials_and_clears_api_overrides(isolated_ssh, monkeypatch):
    credentials = {"claudeAiOauth": {"accessToken": "claude-token"}}
    security.set_secret_json("claude-account:work:credentials", credentials)
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_claude_account_profile(
        ClaudeAccountProfile(
            name="work",
            credentials_ref="claude-account:work:credentials",
            identity="claude-login-work",
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_claude_sync_state(
        monkeypatch,
        isolated_ssh,
        settings={
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "old-token",
                "ANTHROPIC_API_KEY": "old-token",
                "ANTHROPIC_BASE_URL": "https://relay.example.com",
            },
            "model": "deepseek-chat",
        },
        config={"primaryApiKey": "old-token"},
    )

    message = sync_manager.sync_claude_account_to_server("remote", "work")

    assert writes["credentials"][-1] == (fake_client, credentials, profile_manager.list_ssh_profiles()[0])
    assert "env" not in writes["settings"][-1][1]
    assert writes["settings"][-1][1]["model"] == "claude-opus-5"
    assert "primaryApiKey" not in writes["config"][-1][1]
    assert "ssh.example.com" in message


def test_sync_claude_account_to_root_downgrades_existing_bypass_permissions(isolated_ssh, monkeypatch):
    credentials = {"claudeAiOauth": {"accessToken": "claude-token"}}
    security.set_secret_json("claude-account:work:credentials", credentials)
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="root"))
    profile_manager.save_claude_account_profile(
        ClaudeAccountProfile(
            name="work",
            credentials_ref="claude-account:work:credentials",
            identity="claude-login-work",
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_claude_sync_state(
        monkeypatch,
        isolated_ssh,
        settings={"permissions": {"defaultMode": "bypassPermissions"}},
        vscode={"claudeCode.initialPermissionMode": "bypassPermissions"},
    )

    message = sync_manager.sync_claude_account_to_server("remote", "work")

    assert writes["settings"][-1][1]["permissions"]["defaultMode"] == "dontAsk"
    assert writes["settings"][-1][1]["skipDangerousModePermissionPrompt"] is False
    assert writes["vscode"][-1]["claudeCode.initialPermissionMode"] == "default"
    assert writes["vscode"][-1]["claudeCode.allowDangerouslySkipPermissions"] is False
    assert "已兼容 root 登录" in message


def test_remote_vscode_root_safety_preserves_each_existing_channel(isolated_ssh, monkeypatch):
    _state, _writes = _install_claude_sync_state(monkeypatch, isolated_ssh)
    paths = [path.replace("~", "/home/test", 1) for path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS]
    files = isolated_ssh.remote_files
    for index, path in enumerate(paths):
        files[path] = json.dumps(
            {
                f"KEEP_{index}": f"channel-{index}",
                "claudeCode.initialPermissionMode": "bypassPermissions",
                "claudeCode.allowDangerouslySkipPermissions": True,
            }
        )

    changed = sync_manager._sync_remote_vscode_root_safety(
        object(),
        SSHProfile(name="remote", host="ssh.example.com", username="root"),
    )

    assert changed is True
    for index, path in enumerate(paths):
        written = json.loads(files[path])
        assert written[f"KEEP_{index}"] == f"channel-{index}"
        assert written["claudeCode.initialPermissionMode"] == "dontAsk"
        assert written["claudeCode.allowDangerouslySkipPermissions"] is False
        assert all(f"KEEP_{other}" not in written for other in range(3) if other != index)


def test_sync_claude_account_clears_each_existing_vscode_channel_independently(
    isolated_ssh,
    monkeypatch,
):
    credentials = {"claudeAiOauth": {"accessToken": "claude-token"}}
    security.set_secret_json("claude-account:work:credentials", credentials)
    profile_manager.save_ssh_profile(
        SSHProfile(name="remote", host="ssh.example.com", username="ubuntu")
    )
    profile_manager.save_claude_account_profile(
        ClaudeAccountProfile(
            name="work",
            credentials_ref="claude-account:work:credentials",
            identity="claude-login-work",
        )
    )
    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda _profile: fake_client)
    _state, _writes = _install_claude_sync_state(
        monkeypatch,
        isolated_ssh,
        settings={"model": "claude-opus-4"},
    )
    paths = [path.replace("~", "/home/test", 1) for path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS]
    files = isolated_ssh.remote_files
    for index, path in enumerate(paths):
        files[path] = json.dumps(
            {
                f"KEEP_{index}": f"channel-{index}",
                "claudeCode.selectedModel": f"third-party-{index}",
                "claudeCode.initialPermissionMode": "bypassPermissions",
                "claudeCode.allowDangerouslySkipPermissions": True,
            }
        )

    message = sync_manager.sync_claude_account_to_server("remote", "work")

    assert "ssh.example.com" in message
    assert "已兼容 root 登录" not in message
    for index, path in enumerate(paths):
        written = json.loads(files[path])
        assert written[f"KEEP_{index}"] == f"channel-{index}"
        assert written["claudeCode.selectedModel"] == "claude-opus-4"
        assert written["claudeCode.initialPermissionMode"] == "default"
        assert written["claudeCode.allowDangerouslySkipPermissions"] is False
        assert all(f"KEEP_{other}" not in written for other in range(3) if other != index)


def test_sync_claude_account_vscode_readback_failure_rolls_back_every_channel(
    isolated_ssh,
    monkeypatch,
):
    credentials = {"claudeAiOauth": {"accessToken": "claude-token"}}
    security.set_secret_json("claude-account:work:credentials", credentials)
    profile_manager.save_ssh_profile(
        SSHProfile(name="remote", host="ssh.example.com", username="ubuntu")
    )
    profile_manager.save_claude_account_profile(
        ClaudeAccountProfile(
            name="work",
            credentials_ref="claude-account:work:credentials",
            identity="claude-login-work",
        )
    )
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda _profile: object())
    _state, _writes = _install_claude_sync_state(
        monkeypatch,
        isolated_ssh,
        settings={"model": "claude-opus-5"},
    )
    paths = [path.replace("~", "/home/test", 1) for path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS]
    files = isolated_ssh.remote_files
    for index, path in enumerate(paths):
        files[path] = json.dumps(
            {
                f"KEEP_{index}": f"channel-{index}",
                "claudeCode.selectedModel": f"relay-{index}",
            },
            indent=index + 1,
        )
    before = {path: files[path] for path in paths}
    original_write_json = remote_config.write_remote_json

    def corrupt_second_readback(client, path, data, file_mode=None):
        original_write_json(client, path, data, file_mode=file_mode)
        if path == paths[1]:
            files[path] = json.dumps({"unexpected": True})

    monkeypatch.setattr(remote_config, "write_remote_json", corrupt_second_readback)

    with pytest.raises(RuntimeError, match="回读不一致"):
        sync_manager.sync_claude_account_to_server("remote", "work")

    assert {path: files[path] for path in paths} == before


def test_sync_codex_account_to_server_writes_chatgpt_auth_and_official_config(isolated_ssh, monkeypatch):
    auth = {"auth_mode": "api_key", "OPENAI_API_KEY": "old-key", "tokens": {"id_token": "chatgpt-token"}}
    security.set_secret_json("codex-account:work:auth_json", auth)
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    profile_manager.save_codex_account_profile(
        CodexAccountProfile(
            name="work",
            auth_json_ref="codex-account:work:auth_json",
            identity="codex-login-work",
        )
    )

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    state, writes = _install_codex_sync_state(
        monkeypatch,
        isolated_ssh,
        auth={"auth_mode": "apikey", "OPENAI_API_KEY": "old"},
        config={
            "model_provider": "deepseek",
            "model_providers": {
                "deepseek": {"env_key": "DEEPSEEK_API_KEY"},
            },
        },
        dotenv=(
            "OPENAI_API_KEY=old\n"
            "DEEPSEEK_API_KEY=deepseek-old\n"
            "GEMINI_API_KEY=gemini-keep\n"
            "KEEP=yes\n"
        ),
        persistent=(
            "OPENAI_API_KEY=old\n"
            "DEEPSEEK_API_KEY=deepseek-old\n"
            "GEMINI_API_KEY=gemini-keep\n"
        ),
    )
    monkeypatch.setattr(sync_manager, "_remote_codex_login_status", lambda *args, **kwargs: (True, "Logged in using ChatGPT"))

    message = sync_manager.sync_codex_account_to_server("remote", "work")

    assert writes["auth"][-1][0] is fake_client
    assert writes["auth"][-1][1]["auth_mode"] == "chatgpt"
    assert "OPENAI_API_KEY" not in writes["auth"][-1][1]
    assert writes["auth"][-1][2].name == "remote"
    assert writes["config"][-1][1]["model_provider"] == "openai"
    assert writes["config"][-1][1]["cli_auth_credentials_store"] == "file"
    assert "OPENAI_API_KEY" not in state["dotenv"]
    assert "DEEPSEEK_API_KEY" not in state["dotenv"]
    assert "GEMINI_API_KEY=gemini-keep" in state["dotenv"]
    assert set(writes["deleted"][-1][1]) == {
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    }
    assert "ssh.example.com" in message


def test_sync_codex_account_rolls_back_nested_config_and_env_on_validation_failure(isolated_ssh, monkeypatch):
    auth = {"auth_mode": "chatgpt", "tokens": {"id_token": "new-token"}}
    security.set_secret_json("codex-account:work:auth_json", auth)
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    profile_manager.save_codex_account_profile(
        CodexAccountProfile(
            name="work",
            auth_json_ref="codex-account:work:auth_json",
            identity="work",
        )
    )
    old_auth = {"auth_mode": "apikey", "OPENAI_API_KEY": "old"}
    old_config = {
        "model_provider": "custom",
        "model_providers": {"custom": {"wire_api": "chat", "env_key": "CUSTOM_KEY"}},
    }
    old_auth_text = '{\n  "auth_mode" : "apikey",\n  "OPENAI_API_KEY" : "old"\n}\n'
    old_config_text = (
        "# Preserve this user comment and the original formatting.\n"
        'model_provider = "custom"\n\n'
        "[model_providers.custom]\n"
        'wire_api = "chat"\n'
        'env_key = "CUSTOM_KEY"\n'
    )
    state = {
        "auth": old_auth,
        "config": old_config,
        "auth_text": old_auth_text,
        "config_text": old_config_text,
        "codex_env": "CUSTOM_KEY=old\nKEEP=yes\n",
        "persistent": "export CUSTOM_KEY=old\n",
    }
    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "_remote_home", lambda client: "/home/test")
    def read_remote_text(client, path):
        if path.endswith("auth.json"):
            return state["auth_text"]
        if path.endswith("config.toml"):
            return state["config_text"]
        if path.endswith("/.env"):
            return state["codex_env"]
        if path.endswith(".api_switcher_env"):
            return state["persistent"]
        return None

    monkeypatch.setattr(remote_config, "read_remote_text", read_remote_text)
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda *args, **kwargs: state["auth"])
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda *args, **kwargs: state["config"])
    monkeypatch.setattr(remote_config, "read_remote_codex_env", lambda *args, **kwargs: state["codex_env"])
    def write_remote_codex_auth(client, data, profile=None):
        state["auth"] = data
        state["auth_text"] = json.dumps(data)

    def write_remote_codex_config(client, data, profile=None):
        state["config"] = data
        state["config_text"] = "model_provider = \"openai\"\n"

    monkeypatch.setattr(remote_config, "write_remote_codex_auth", write_remote_codex_auth)
    monkeypatch.setattr(remote_config, "write_remote_codex_config", write_remote_codex_config)
    monkeypatch.setattr(remote_config, "write_remote_codex_env", lambda client, data, profile=None: state.__setitem__("codex_env", data))
    def write_remote_text(client, path, data, file_mode=None):
        if path.endswith("auth.json"):
            state["auth_text"] = data
            state["auth"] = json.loads(data)
        elif path.endswith("config.toml"):
            state["config_text"] = data
            import tomllib

            state["config"] = tomllib.loads(data)
        elif path.endswith("/.env"):
            state["codex_env"] = data
        else:
            state["persistent"] = data

    monkeypatch.setattr(remote_config, "write_remote_text", write_remote_text)
    monkeypatch.setattr(remote_config, "delete_remote_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        persistent_env,
        "delete_remote_user_env",
        lambda client, names: state.__setitem__(
            "persistent",
            codex_env.merge_codex_env_text(state["persistent"], deletes=names),
        ),
    )
    monkeypatch.setattr(
        sync_manager,
        "_remote_codex_login_status",
        lambda *args, **kwargs: (False, "Not logged in"),
    )

    with pytest.raises(RuntimeError, match="登录状态校验失败"):
        sync_manager.sync_codex_account_to_server("remote", "work")

    assert state["auth"] == old_auth
    assert state["config"] == old_config
    assert state["config"]["model_providers"]["custom"]["wire_api"] == "chat"
    assert state["auth_text"] == old_auth_text
    assert state["config_text"] == old_config_text
    assert state["codex_env"] == "CUSTOM_KEY=old\nKEEP=yes\n"
    assert state["persistent"] == "export CUSTOM_KEY=old\n"


def test_clear_remote_claude_api_info_removes_overrides_and_env(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    fake_client = object()
    deleted = {}
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    _state, writes = _install_claude_sync_state(
        monkeypatch,
        isolated_ssh,
        settings={
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "sk-old",
                "ANTHROPIC_API_KEY": "sk-old",
                "ANTHROPIC_BASE_URL": "https://relay.example.com",
                "KEEP_ME": "yes",
            },
            "model": "deepseek-chat",
            "effortLevel": "unsupported",
        },
        config={"primaryApiKey": "sk-old"},
    )
    persistent_path = "/home/test/.api_switcher_env"
    isolated_ssh.remote_files[persistent_path] = (
        "export ANTHROPIC_AUTH_TOKEN='sk-old'\n"
        "export ANTHROPIC_API_KEY='sk-old'\n"
        "export KEEP_ENV='yes'\n"
    )

    def delete_remote_env(_client, names):
        names = tuple(names)
        deleted["names"] = names
        isolated_ssh.remote_files[persistent_path] = persistent_env._remove_env_exports(
            isolated_ssh.remote_files[persistent_path],
            names,
        )

    monkeypatch.setattr(
        persistent_env,
        "delete_remote_user_env",
        delete_remote_env,
    )

    message = sync_manager.clear_remote_api_info("remote", "claude")

    assert writes["settings"][-1][0] is fake_client
    assert writes["settings"][-1][1]["env"] == {"KEEP_ME": "yes"}
    assert writes["settings"][-1][1]["model"] == "claude-opus-5"
    assert writes["settings"][-1][1]["effortLevel"] == "high"
    assert "primaryApiKey" not in writes["config"][-1][1]
    assert set(deleted["names"]).issuperset({"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"})
    assert isolated_ssh.remote_files[persistent_path] == "export KEEP_ENV='yes'\n"
    assert "Claude API 信息已清除" in message


def test_clear_remote_codex_api_info_removes_active_provider_auth_and_env(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    fake_client = object()
    remote_config_data = {
        "model": "deepseek-v4-flash",
        "model_provider": "deepseek",
        "model_providers": {
            "deepseek": {
                "base_url": "https://api.deepseek.com",
                "env_key": "DEEPSEEK_API_KEY",
                "wire_api": "responses",
            },
            "other": {"base_url": "https://other.example.com/v1", "env_key": "OTHER_KEY"},
        },
    }
    remote_auth = {
        "auth_mode": "apikey",
        "OPENAI_API_KEY": "sk-old",
        "tokens": {"id_token": "chatgpt-token"},
    }
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    state, writes = _install_codex_sync_state(
        monkeypatch,
        isolated_ssh,
        config=remote_config_data,
        auth=remote_auth,
        dotenv="DEEPSEEK_API_KEY=sk-old\nOTHER_KEY=keep\n",
        persistent="export DEEPSEEK_API_KEY='sk-old'\nexport OTHER_KEY='keep'\n",
    )

    message = sync_manager.clear_remote_api_info("remote", "codex")

    cleaned_config = writes["config"][-1][1]
    assert cleaned_config["model_provider"] == "openai"
    assert "model" not in cleaned_config
    assert "cli_auth_credentials_store" not in cleaned_config
    assert "openai" not in cleaned_config["model_providers"]
    assert cleaned_config["model_providers"]["other"]["env_key"] == "OTHER_KEY"
    assert writes["auth"][-1][1]["auth_mode"] == "chatgpt"
    assert "OPENAI_API_KEY" not in writes["auth"][-1][1]
    assert set(writes["deleted"][-1][1]).issuperset({"OPENAI_API_KEY", "DEEPSEEK_API_KEY"})
    assert "OTHER_KEY" not in writes["deleted"][-1][1]
    assert state["dotenv"] == "OTHER_KEY=keep\n"
    assert isolated_ssh.remote_files["/home/test/.api_switcher_env"] == "export OTHER_KEY='keep'\n"
    assert "Codex API 信息已清除" in message


def test_inspect_remote_configs_marks_importable_and_skipped_configs(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_settings",
        lambda client, profile=None: {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "sk-remote",
                "ANTHROPIC_BASE_URL": "https://relay.example.com",
            },
            "model": "claude-opus-5",
        },
    )
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda client, profile=None: {})
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_credentials",
        lambda client, profile=None: {"claudeAiOauth": {"accessToken": "claude-account-token"}},
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda client, profile=None, **kwargs: {
            "model_provider": "openai",
            "model": "gpt-5.5",
            "cli_auth_credentials_store": "file",
        },
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda client, profile=None, **kwargs: {
            "OPENAI_API_KEY": "sk-openai",
            "tokens": {"id_token": "codex-account-token"},
        },
    )

    candidates = sync_manager.inspect_remote_configs("remote")

    assert [candidate.kind for candidate in candidates] == ["claude", "claude_account", "codex", "codex_account"]
    assert candidates[0].importable is True
    assert candidates[0].provider == "custom"
    assert candidates[0].has_api_key is True
    assert "可导入" in candidates[0].reason
    assert candidates[1].importable is True
    assert candidates[1].category == "account"
    assert candidates[2].importable is False
    assert candidates[2].provider == "openai"
    assert "官方 OpenAI" in candidates[2].reason
    assert candidates[3].importable is True
    assert candidates[3].category == "account"


def test_inspect_remote_configs_keeps_codex_visible_when_claude_read_fails(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_settings",
        lambda client, profile=None: (_ for _ in ()).throw(RuntimeError("permission denied")),
    )
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_claude_credentials", lambda client, profile=None: {})
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda client, profile=None: {
            "model_provider": "deepseek",
            "model": "deepseek-v4-flash",
            "model_providers": {"deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com"}},
        },
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda client, profile=None: {"DEEPSEEK_API_KEY": "sk-deepseek"},
    )

    candidates = sync_manager.inspect_remote_configs("remote")

    assert candidates[0].kind == "claude"
    assert candidates[0].importable is False
    assert "读取失败" in candidates[0].reason
    assert candidates[2].kind == "codex"
    assert candidates[2].importable is True
    assert candidates[2].has_api_key is True
    assert candidates[2].provider_label == "DeepSeek"


def test_pull_official_accounts_from_server(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_credentials",
        lambda client, profile=None: {"claudeAiOauth": {"accessToken": "claude-token"}},
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda client, profile=None, **kwargs: {"tokens": {"id_token": "codex-token"}, "auth_mode": "chatgpt"},
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda client, profile=None, **kwargs: {"cli_auth_credentials_store": "file"},
    )

    claude_message = sync_manager.pull_remote_config_from_server("remote", "claude_account")
    codex_message = sync_manager.pull_remote_config_from_server("remote", "codex_account")

    assert "Claude 账号" in claude_message
    assert len(profile_manager.list_claude_account_profiles()) == 1
    assert security.get_secret_json(profile_manager.list_claude_account_profiles()[0].credentials_ref)["claudeAiOauth"]["accessToken"] == "claude-token"
    assert "Codex 账号" in codex_message
    assert len(profile_manager.list_codex_account_profiles()) == 1
    assert security.get_secret_json(profile_manager.list_codex_account_profiles()[0].auth_json_ref)["auth_mode"] == "chatgpt"


def test_pull_codex_account_rejects_keyring_even_with_stale_auth_file(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda client, profile=None, **kwargs: {"cli_auth_credentials_store": "keyring"},
    )
    stale_reads = []
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *args, **kwargs: stale_reads.append(True) or {"tokens": {"id_token": "stale"}},
    )
    monkeypatch.setattr(
        sync_manager,
        "_remote_codex_login_status",
        lambda *args, **kwargs: (None, "codex CLI not found"),
    )

    with pytest.raises(ValueError, match="keyring.*auth.json"):
        sync_manager.pull_codex_account_from_server("remote")

    assert stale_reads == []
    assert profile_manager.list_codex_account_profiles() == []


def test_pull_codex_account_invalid_auth_raises_instead_of_returning_success(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda *args, **kwargs: {"cli_auth_credentials_store": "file"},
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *args, **kwargs: {"tokens": {"account_id": "metadata-only"}},
    )

    with pytest.raises(ValueError, match="不可导入"):
        sync_manager.pull_codex_account_from_server("remote")


def test_pull_codex_account_rolls_back_secret_when_profile_save_fails(isolated_ssh, monkeypatch):
    secret_store = isolated_ssh
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda *args, **kwargs: {"cli_auth_credentials_store": "file"},
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *args, **kwargs: {"auth_mode": "chatgpt", "tokens": {"id_token": "remote-token"}},
    )
    original_save_store = profile_manager._save_store

    def fail_profile_write(store, *args, **kwargs):
        if store.get("codex_account_profiles"):
            raise OSError("disk full")
        return original_save_store(store, *args, **kwargs)

    monkeypatch.setattr(profile_manager, "_save_store", fail_profile_write)

    with pytest.raises(OSError, match="disk full"):
        sync_manager.pull_codex_account_from_server("remote")

    assert profile_manager.list_codex_account_profiles() == []
    assert not any(key.startswith("codex-account:") for key in secret_store)


def test_remote_git_login_syncs_identity_and_gh_token(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    local_outputs = {
        ("git", "--version"): "git version 2.45.0\n",
        ("git", "config", "--global", "--get", "user.name"): "Local User\n",
        ("git", "config", "--global", "--get", "user.email"): "local@example.com\n",
        ("gh", "--version"): "gh version 2.0.0\n",
        ("gh", "api", "user", "--jq", ".login"): "local-user\n",
        ("gh", "auth", "token"): "gho_secret\n",
    }

    def fake_run(args, **kwargs):
        output = local_outputs.get(tuple(args), "")
        return subprocess.CompletedProcess(args, 0 if output else 1, output, "")

    remote_commands = []
    remote_inputs = []
    fake_client = object()

    def fake_execute(client, command, timeout=30, input_data=None, log_command=True, get_pty=False):
        remote_commands.append(command)
        remote_inputs.append(input_data)
        if "__git_available" in command:
            return 0, "\n".join([
                "__git_available=1",
                "__user_name=",
                "__user_email=",
                "__gh_available=1",
                "__gh_logged_in=0",
                "__gh_summary=not logged in",
            ]), ""
        if "gh auth login" in command:
            return 0, "logged in", ""
        if "git config --global user.name" in command:
            return 0, "", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(remote_git_login.subprocess, "run", fake_run)
    monkeypatch.setattr(remote_git_login.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_git_login.ssh_manager, "execute_command_with_status", fake_execute)

    message = remote_git_login.sync_git_login_to_server("remote")

    assert "已同步 Git 身份" in message
    assert "已同步 GitHub CLI 登录" in message
    assert any("git config --global user.name" in command for command in remote_commands)
    assert any("gh auth login" in command for command in remote_commands)
    assert "gho_secret\n" in remote_inputs


def test_remote_git_login_installs_gh_when_missing(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    local_outputs = {
        ("git", "--version"): "git version 2.45.0\n",
        ("git", "config", "--global", "--get", "user.name"): "Local User\n",
        ("git", "config", "--global", "--get", "user.email"): "local@example.com\n",
        ("gh", "--version"): "gh version 2.0.0\n",
        ("gh", "api", "user", "--jq", ".login"): "local-user\n",
        ("gh", "auth", "token"): "gho_secret\n",
    }

    def fake_run(args, **kwargs):
        output = local_outputs.get(tuple(args), "")
        return subprocess.CompletedProcess(args, 0 if output else 1, output, "")

    probe_count = 0
    remote_commands = []

    def fake_execute(client, command, timeout=30, input_data=None, log_command=True, get_pty=False):
        nonlocal probe_count
        remote_commands.append(command)
        if "__git_available" in command:
            probe_count += 1
            gh_available = "0" if probe_count == 1 else "1"
            return 0, "\n".join([
                "__git_available=1",
                "__user_name=",
                "__user_email=",
                f"__gh_available={gh_available}",
                "__gh_logged_in=0",
                "__gh_summary=not logged in",
            ]), ""
        if "gh --version" in command and "apt install" in command:
            return 0, "gh version 2.0.0", ""
        if "gh auth login" in command:
            assert input_data == "gho_secret\n"
            return 0, "logged in", ""
        return 0, "", ""

    monkeypatch.setattr(remote_git_login.subprocess, "run", fake_run)
    monkeypatch.setattr(remote_git_login.ssh_manager, "connect", lambda profile: object())
    monkeypatch.setattr(remote_git_login.ssh_manager, "execute_command_with_status", fake_execute)

    message = remote_git_login.sync_git_login_to_server("remote")

    assert "已自动安装 GitHub CLI" in message
    assert "已同步 GitHub CLI 登录" in message
    assert any("apt install -y gh" in command for command in remote_commands)


def test_remote_git_login_installs_gh_on_windows_remote(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="win.example.com", username="zzy"))

    local_outputs = {
        ("git", "--version"): "git version 2.45.0\n",
        ("git", "config", "--global", "--get", "user.name"): "Local User\n",
        ("git", "config", "--global", "--get", "user.email"): "local@example.com\n",
        ("gh", "--version"): "gh version 2.0.0\n",
        ("gh", "api", "user", "--jq", ".login"): "local-user\n",
        ("gh", "auth", "token"): "gho_secret\n",
    }

    def fake_run(args, **kwargs):
        output = local_outputs.get(tuple(args), "")
        return subprocess.CompletedProcess(args, 0 if output else 1, output, "")

    probe_count = 0
    remote_commands = []

    def fake_execute(client, command, timeout=30, input_data=None, log_command=True, get_pty=False):
        nonlocal probe_count
        remote_commands.append(command)
        if "set +e" in command:
            return 1, "", "'set' is not recognized"
        if "__os=windows" in command:
            probe_count += 1
            gh_available = "0" if probe_count == 1 else "1"
            return 0, "\n".join([
                "__os=windows",
                "__git_available=1",
                "__user_name=",
                "__user_email=",
                f"__gh_available={gh_available}",
                "__gh_logged_in=0",
                "__gh_summary=not logged in",
            ]), ""
        if "winget install --id GitHub.cli" in command:
            return 0, "gh version 2.0.0", ""
        if "gh auth login" in command:
            assert input_data == "gho_secret\n"
            return 0, "logged in", ""
        if "git config --global user.name" in command:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(remote_git_login.subprocess, "run", fake_run)
    monkeypatch.setattr(remote_git_login.ssh_manager, "connect", lambda profile: object())
    monkeypatch.setattr(remote_git_login.ssh_manager, "execute_command_with_status", fake_execute)

    message = remote_git_login.sync_git_login_to_server("remote")

    assert "已自动安装 GitHub CLI" in message
    assert "已同步 GitHub CLI 登录" in message
    assert any("winget install --id GitHub.cli" in command for command in remote_commands)
    assert any("powershell.exe" in command and "gh auth login" in command for command in remote_commands)


def test_remote_git_login_installs_local_gh_on_windows(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    installed = False
    local_commands = []

    def fake_run(args, **kwargs):
        nonlocal installed
        local_commands.append(tuple(args))
        if tuple(args) == ("git", "--version"):
            return subprocess.CompletedProcess(args, 0, "git version 2.45.0\n", "")
        if tuple(args) == ("git", "config", "--global", "--get", "user.name"):
            return subprocess.CompletedProcess(args, 0, "Local User\n", "")
        if tuple(args) == ("git", "config", "--global", "--get", "user.email"):
            return subprocess.CompletedProcess(args, 0, "local@example.com\n", "")
        if tuple(args) == ("gh", "--version"):
            return subprocess.CompletedProcess(args, 0 if installed else 1, "gh version 2.0.0\n" if installed else "", "")
        if tuple(args) == ("winget", "--version"):
            return subprocess.CompletedProcess(args, 0, "v1.9.0\n", "")
        if args[:5] == ["winget", "install", "--id", "GitHub.cli", "-e"]:
            installed = True
            return subprocess.CompletedProcess(args, 0, "installed\n", "")
        if tuple(args) == ("gh", "api", "user", "--jq", ".login"):
            return subprocess.CompletedProcess(args, 1, "", "")
        if tuple(args) == ("gh", "auth", "token"):
            return subprocess.CompletedProcess(args, 1, "", "")
        if tuple(args) == ("gh", "auth", "status", "-h", "github.com"):
            return subprocess.CompletedProcess(args, 1, "", "")
        return subprocess.CompletedProcess(args, 1, "", "")

    def fake_execute(client, command, timeout=30, input_data=None, log_command=True, get_pty=False):
        if "__git_available" in command:
            return 0, "\n".join([
                "__git_available=1",
                "__user_name=",
                "__user_email=",
                "__gh_available=1",
                "__gh_logged_in=0",
                "__gh_summary=not logged in",
            ]), ""
        if "git config --global user.name" in command:
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(remote_git_login.platform, "system", lambda: "Windows")
    monkeypatch.setattr(remote_git_login.subprocess, "run", fake_run)
    monkeypatch.setattr(remote_git_login.ssh_manager, "connect", lambda profile: object())
    monkeypatch.setattr(remote_git_login.ssh_manager, "execute_command_with_status", fake_execute)

    message = remote_git_login.sync_git_login_to_server("remote")

    assert installed is True
    assert any(command[:5] == ("winget", "install", "--id", "GitHub.cli", "-e") for command in local_commands)
    assert "已自动安装本机 GitHub CLI" in message
    assert "请先在本机执行 gh auth login" in message


def test_remote_git_login_imports_from_server_to_local(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    local_commands = []
    local_inputs = []

    def fake_run(args, **kwargs):
        local_commands.append(tuple(args))
        if tuple(args) == ("git", "--version"):
            return subprocess.CompletedProcess(args, 0, "git version 2.45.0\n", "")
        if tuple(args) == ("gh", "--version"):
            return subprocess.CompletedProcess(args, 0, "gh version 2.0.0\n", "")
        if tuple(args) == ("git", "config", "--global", "user.name", "Remote User"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if tuple(args) == ("git", "config", "--global", "user.email", "remote@example.com"):
            return subprocess.CompletedProcess(args, 0, "", "")
        if tuple(args) == ("gh", "auth", "setup-git", "--hostname", "github.com"):
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "")

    def fake_run_with_input(args, input_data, timeout=60):
        local_commands.append(tuple(args))
        local_inputs.append(input_data)
        if tuple(args) == ("gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--with-token"):
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    def fake_execute(client, command, timeout=30, input_data=None, log_command=True, get_pty=False):
        if "__git_available" in command:
            return 0, "\n".join([
                "__os=Linux",
                "__git_available=1",
                "__user_name=Remote User",
                "__user_email=remote@example.com",
                "__gh_available=1",
                "__gh_logged_in=1",
                "__gh_summary=Logged in",
            ]), ""
        if command.strip() == "gh auth token":
            return 0, "gho_remote_secret\n", ""
        return 0, "", ""

    monkeypatch.setattr(remote_git_login.subprocess, "run", fake_run)
    monkeypatch.setattr(remote_git_login, "_run_local_with_input", fake_run_with_input)
    monkeypatch.setattr(remote_git_login.ssh_manager, "connect", lambda profile: object())
    monkeypatch.setattr(remote_git_login.ssh_manager, "execute_command_with_status", fake_execute)

    message = remote_git_login.sync_git_login_from_server("remote")

    assert "已导入 Git 身份" in message
    assert "已导入远端 GitHub CLI 登录" in message
    assert ("git", "config", "--global", "user.name", "Remote User") in local_commands
    assert ("git", "config", "--global", "user.email", "remote@example.com") in local_commands
    assert "gho_remote_secret\n" in local_inputs


def test_remote_git_login_status_summary(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    def fake_run(args, **kwargs):
        outputs = {
            ("git", "--version"): "git version 2.45.0\n",
            ("git", "config", "--global", "--get", "user.name"): "Local User\n",
            ("git", "config", "--global", "--get", "user.email"): "local@example.com\n",
            ("gh", "--version"): "",
        }
        output = outputs.get(tuple(args), "")
        return subprocess.CompletedProcess(args, 0 if output else 1, output, "")

    def fake_execute(client, command, timeout=30, input_data=None, log_command=True, get_pty=False):
        return 0, "\n".join([
            "__git_available=1",
            "__user_name=Remote User",
            "__user_email=remote@example.com",
            "__gh_available=0",
            "__gh_logged_in=0",
            "__gh_summary=gh not installed",
        ]), ""

    monkeypatch.setattr(remote_git_login.subprocess, "run", fake_run)
    monkeypatch.setattr(remote_git_login.ssh_manager, "connect", lambda profile: object())
    monkeypatch.setattr(remote_git_login.ssh_manager, "execute_command_with_status", fake_execute)

    status = remote_git_login.inspect_git_login("remote")

    assert status.local_user_email == "local@example.com"
    assert status.remote_user_name == "Remote User"
    assert status.remote_gh_available is False
    assert "本机" in status.summary()


def test_pull_codex_from_server_skips_empty_api_key(isolated_ssh, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))

    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda client, profile=None: {"model_provider": "deepseek", "model": "deepseek-v4-flash"},
    )
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {"DEEPSEEK_API_KEY": "  "})

    message = sync_manager.pull_codex_from_server("remote")

    assert "没有 API Key" in message
    assert profile_manager.list_codex_profiles() == []


def test_ssh_connect_reconnects_when_cached_profile_details_change(isolated_ssh, monkeypatch):
    import core.ssh_manager as ssh_core

    class _ActiveTransport:
        def is_active(self):
            return True

    class _CachedSSHClient:
        def __init__(self):
            self.closed = False

        def get_transport(self):
            return _ActiveTransport()

        def close(self):
            self.closed = True

    class _ConnectingSSHClient:
        instances = []

        def __init__(self):
            self.kwargs = None
            self.instances.append(self)

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **kwargs):
            self.kwargs = kwargs

        def get_transport(self):
            return _ActiveTransport()

    security.set_secret("ssh:remote:password", "secret-password")
    manager = SSHManager()
    old_profile = SSHProfile(
        name="remote",
        host="old.example.com",
        username="root",
        auth_type="password",
        password_ref="ssh:remote:password",
    )
    new_profile = SSHProfile(
        name="remote",
        host="new.example.com",
        username="root",
        auth_type="password",
        password_ref="ssh:remote:password",
    )
    cached_client = _CachedSSHClient()
    manager._clients["remote"] = cached_client
    manager._client_signatures["remote"] = manager._connection_signature(old_profile)

    monkeypatch.setattr(ssh_core.paramiko, "SSHClient", _ConnectingSSHClient)

    client = manager.connect(new_profile, timeout=1, max_retries=1)

    assert cached_client.closed
    assert client.kwargs["hostname"] == "new.example.com"
    assert manager._clients["remote"] is client
    assert manager._client_signatures["remote"] == manager._connection_signature(new_profile)


def test_ssh_connect_uses_secret_override_without_keyring_or_cached_client(
    isolated_ssh,
    monkeypatch,
):
    import core.ssh_manager as ssh_core

    class _ActiveTransport:
        def is_active(self):
            return True

    class _CachedSSHClient:
        def __init__(self):
            self.closed = False

        def get_transport(self):
            return _ActiveTransport()

        def close(self):
            self.closed = True

    class _EphemeralSSHClient:
        def __init__(self):
            self.kwargs = None

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **kwargs):
            self.kwargs = kwargs

        def get_transport(self):
            return _ActiveTransport()

    ref = "ssh:remote:password"
    profile = SSHProfile(
        name="remote",
        host="new.example.com",
        username="root",
        auth_type="password",
        password_ref=ref,
    )
    manager = SSHManager()
    cached_client = _CachedSSHClient()
    manager._clients[profile.name] = cached_client
    manager._client_signatures[profile.name] = manager._connection_signature(profile)
    monkeypatch.setattr(ssh_core.paramiko, "SSHClient", _EphemeralSSHClient)
    monkeypatch.setattr(
        security,
        "get_secret",
        lambda _ref: (_ for _ in ()).throw(AssertionError("override must bypass keyring")),
    )

    client = manager.connect(
        profile,
        timeout=1,
        max_retries=1,
        secret_overrides={ref: "temporary-password"},
    )

    assert client.kwargs["password"] == "temporary-password"
    assert manager._clients[profile.name] is cached_client
    assert cached_client.closed is False


def test_ssh_connect_closes_every_failed_retry_client(isolated_ssh, monkeypatch):
    import core.ssh_manager as ssh_core

    clients = []

    class _FailingSSHClient:
        def __init__(self):
            self.closed = False
            clients.append(self)

        def set_missing_host_key_policy(self, _policy):
            pass

        def connect(self, **_kwargs):
            raise OSError("connection refused")

        def close(self):
            self.closed = True

    ref = "ssh:failed:password"
    profile = SSHProfile(
        name="failed",
        host="ssh.example.com",
        username="root",
        auth_type="password",
        password_ref=ref,
    )
    manager = SSHManager()
    monkeypatch.setattr(ssh_core.paramiko, "SSHClient", _FailingSSHClient)
    monkeypatch.setattr(ssh_core.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="connection refused"):
        manager.connect(
            profile,
            timeout=1,
            max_retries=2,
            secret_overrides={ref: "temporary-password"},
        )

    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_ssh_connection_test_closes_ephemeral_override_client(monkeypatch):
    class _Client:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    manager = SSHManager()
    client = _Client()
    captured = {}
    profile = SSHProfile(
        name="remote",
        host="ssh.example.com",
        username="root",
        auth_type="password",
        password_ref="ssh:remote:password",
    )

    def connect(_profile, **kwargs):
        captured.update(kwargs)
        return client

    monkeypatch.setattr(manager, "connect", connect)
    monkeypatch.setattr(
        manager,
        "execute_command",
        lambda *_args, **_kwargs: ("Connection OK\n", ""),
    )

    success, _message = manager.test_connection(
        profile,
        secret_overrides={"ssh:remote:password": "temporary-password"},
    )

    assert success is True
    assert captured["secret_overrides"] == {
        "ssh:remote:password": "temporary-password",
    }
    assert client.closed is True


class _FakeChannel:
    def settimeout(self, timeout):
        self.timeout = timeout


class _FakeReader(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class _FakeWriter:
    def __init__(self, sftp, path):
        self.sftp = sftp
        self.path = path
        self.buffer = bytearray()

    def write(self, data):
        assert isinstance(data, bytes)
        self.buffer.extend(data)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.sftp.files[self.path] = bytes(self.buffer)


class _FakeSFTP:
    def __init__(self):
        self.files = {"/remote.json": b'{"ok": true}'}
        self.open_modes = []
        self.dirs = {"/"}
        self.mkdir_calls = []
        self.chmod_calls = []
        self.rename_calls = []
        self.posix_rename_calls = []

    def get_channel(self):
        return _FakeChannel()

    def normalize(self, path):
        if path == ".":
            return "/home/fallback"
        return path

    def open(self, path, mode):
        self.open_modes.append(mode)
        if "r" in mode:
            if path not in self.files:
                raise FileNotFoundError(path)
            return _FakeReader(self.files[path])
        return _FakeWriter(self, path)

    def rename(self, source, target):
        self.rename_calls.append((source, target))
        self.files[target] = self.files.pop(source)

    def posix_rename(self, source, target):
        self.posix_rename_calls.append((source, target))
        self.files[target] = self.files.pop(source)

    def remove(self, path):
        self.files.pop(path, None)

    def stat(self, path):
        normalized = path.replace("\\", "/")
        if normalized in self.dirs or normalized in self.files:
            return object()
        error = OSError("No such file")
        error.errno = 2
        raise error

    def mkdir(self, path):
        assert "\\" not in path
        normalized = path.replace("\\", "/")
        self.dirs.add(normalized)
        self.mkdir_calls.append(normalized)

    def chmod(self, path, mode):
        assert "\\" not in path
        self.chmod_calls.append((path, mode))

    def close(self):
        self.closed = True


class _FakeClient:
    def __init__(self, sftp, command_outputs=None):
        self.sftp = sftp
        self.command_outputs = list(command_outputs or [])

    def open_sftp(self):
        return self.sftp

    def exec_command(self, command, timeout=None):
        output = self.command_outputs.pop(0) if self.command_outputs else "/home/test"
        return None, _FakeReader(str(output).encode("utf-8")), _FakeReader(b"")


def _expected_remote_hook_script(paths):
    return remote_auto_continue._generate_remote_hook_script(
        paths.settings_path,
        paths.state_dir,
    ).encode("utf-8")


def test_ssh_remote_file_io_uses_binary_sftp_modes():
    manager = SSHManager()
    sftp = _FakeSFTP()
    sftp.files["/bom.json"] = b'\xef\xbb\xbf{"ok": true}'
    sftp.files["/invalid.txt"] = b"ok\xff"
    client = _FakeClient(sftp)

    assert manager.read_remote_file(client, "/remote.json") == '{"ok": true}'
    assert manager.read_remote_file(client, "/bom.json") == '{"ok": true}'
    assert manager.read_remote_file(client, "/invalid.txt") == "ok\ufffd"
    manager.write_remote_file(client, "/written.json", '{"saved": true}')

    assert "rb" in sftp.open_modes
    assert "wb" in sftp.open_modes
    assert sftp.files["/written.json"] == b'{"saved": true}'
    assert sftp.posix_rename_calls
    assert not sftp.rename_calls
    assert all("\\" not in path for path in sftp.mkdir_calls)


def test_ssh_remote_file_explicit_mode_fails_closed_when_chmod_fails():
    class ChmodFailureSFTP(_FakeSFTP):
        def chmod(self, path, mode):
            super().chmod(path, mode)
            raise OSError("chmod denied")

    manager = SSHManager()
    sftp = ChmodFailureSFTP()
    sftp.files["/credentials.json"] = b'{"old": true}'
    client = _FakeClient(sftp)

    with pytest.raises(RuntimeError, match="chmod denied"):
        manager.write_remote_file(
            client,
            "/credentials.json",
            '{"new": true}',
            file_mode=0o600,
        )

    assert sftp.files["/credentials.json"] == b'{"old": true}'


def test_ssh_remote_file_replace_preserves_existing_file_during_fallback():
    class NoOverwriteSFTP(_FakeSFTP):
        def posix_rename(self, source, target):
            self.posix_rename_calls.append((source, target))
            error = OSError("Operation unsupported")
            error.errno = 95
            raise error

        def rename(self, source, target):
            self.rename_calls.append((source, target))
            if target in self.files:
                raise OSError("Failure")
            self.files[target] = self.files.pop(source)

    manager = SSHManager()
    sftp = NoOverwriteSFTP()
    sftp.files["/written.json"] = b'{"old": true}'
    client = _FakeClient(sftp)

    manager.write_remote_file(client, "/written.json", '{"new": true}')

    assert sftp.files["/written.json"] == b'{"new": true}'
    assert not any(path.startswith("/written.json.bak.") for path in sftp.files)
    assert any(source == "/written.json" and target.startswith("/written.json.bak.") for source, target in sftp.rename_calls)


def test_remote_config_reads_json_with_utf8_bom():
    sftp = _FakeSFTP()
    sftp.files["/bom.json"] = b'\xef\xbb\xbf{"ok": true}'
    client = _FakeClient(sftp)

    assert remote_config.read_remote_json(client, "/bom.json") == {"ok": True}


def test_remote_config_ignores_non_object_json():
    sftp = _FakeSFTP()
    sftp.files["/list.json"] = b'["not", "a", "config"]'
    client = _FakeClient(sftp)

    assert remote_config.read_remote_json(client, "/list.json") is None


def test_remote_config_expands_home_and_custom_profile_dirs():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp, command_outputs=["/srv/users/alice"])
    profile = SSHProfile(
        name="remote",
        host="ssh.example.com",
        remote_codex_dir="$HOME/.config/codex",
    )

    remote_config.write_remote_codex_auth(client, {"tokens": {"id_token": "token"}}, profile)

    assert "/srv/users/alice/.config/codex/auth.json" in sftp.files
    assert any(
        path.startswith("/srv/users/alice/.config/codex/auth.json.tmp.")
        for path, mode in sftp.chmod_calls
        if mode == 0o600
    )
    assert ("/srv/users/alice/.config/codex/auth.json", 0o600) in sftp.chmod_calls


def test_remote_config_prefers_codex_home_for_default_profile_dir():
    sftp = _FakeSFTP()
    client = _FakeClient(
        sftp,
        command_outputs=[
            "__API_SWITCHER_CODEX_HOME__/srv/codex-state",
            "/home/test",
        ],
    )
    profile = SSHProfile(
        name="remote",
        host="ssh.example.com",
        remote_codex_dir="~/.codex",
    )

    remote_config.write_remote_codex_auth(
        client,
        {"tokens": {"id_token": "token"}},
        profile,
    )

    assert "/srv/codex-state/auth.json" in sftp.files


def test_remote_config_prefers_claude_config_dir_for_default_profile_dir():
    sftp = _FakeSFTP()
    client = _FakeClient(
        sftp,
        command_outputs=[
            "__API_SWITCHER_CLAUDE_CONFIG_DIR__/srv/claude-state",
            "/home/test",
        ],
    )
    profile = SSHProfile(
        name="remote",
        host="ssh.example.com",
        remote_claude_dir="~/.claude",
    )

    remote_config.write_remote_claude_settings(client, {"model": "sonnet"}, profile)

    assert "/srv/claude-state/settings.json" in sftp.files


def test_remote_config_explicit_claude_dir_wins_over_remote_environment():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp, command_outputs=["/home/test"])
    profile = SSHProfile(
        name="remote",
        host="ssh.example.com",
        remote_claude_dir="/srv/explicit-claude",
    )

    remote_config.write_remote_claude_settings(client, {"model": "sonnet"}, profile)

    assert "/srv/explicit-claude/settings.json" in sftp.files


def test_remote_config_empty_codex_home_falls_back_to_home_dot_codex():
    sftp = _FakeSFTP()
    marker = remote_config._CODEX_HOME_MARKER
    client = _FakeClient(
        sftp,
        command_outputs=[marker, marker, "/home/alice"],
    )
    profile = SSHProfile(
        name="remote",
        host="ssh.example.com",
        remote_codex_dir="~/.codex",
    )

    remote_config.write_remote_codex_auth(
        client,
        {"tokens": {"id_token": "token"}},
        profile,
    )

    assert "/home/alice/.codex/auth.json" in sftp.files


def test_remote_config_explicit_codex_dir_wins_over_codex_home():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp, command_outputs=["/home/test"])
    profile = SSHProfile(
        name="remote",
        host="ssh.example.com",
        remote_codex_dir="/srv/explicit-codex",
    )

    remote_config.write_remote_codex_auth(
        client,
        {"tokens": {"id_token": "token"}},
        profile,
    )

    assert "/srv/explicit-codex/auth.json" in sftp.files


def test_remote_config_strict_json_distinguishes_corruption_from_missing():
    sftp = _FakeSFTP()
    sftp.files["/broken.json"] = b"{not-json"
    client = _FakeClient(sftp)

    with pytest.raises(RuntimeError, match="JSON 格式损坏"):
        remote_config.read_remote_json(client, "/broken.json", strict=True)


def test_remote_config_uses_sftp_home_fallback_when_home_env_is_empty():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp, command_outputs=["", "", "", "", ""])

    remote_config.write_remote_claude_settings(client, {"model": "claude-opus-5"})

    assert "/home/fallback/.claude/settings.json" in sftp.files


def test_remote_vscode_settings_updates_existing_machine_settings():
    sftp = _FakeSFTP()
    settings_path = "/home/test/.vscode-server/data/Machine/settings.json"
    sftp.files[settings_path] = b'{"claudeCode.initialPermissionMode": "bypassPermissions"}'
    client = _FakeClient(sftp)

    settings = remote_config.read_remote_vscode_settings(client)
    assert settings["claudeCode.initialPermissionMode"] == "bypassPermissions"

    remote_config.write_remote_vscode_settings(
        client,
        {
            "claudeCode.initialPermissionMode": "default",
            "claudeCode.allowDangerouslySkipPermissions": False,
        },
    )

    written = json.loads(sftp.files[settings_path].decode("utf-8"))
    assert written["claudeCode.initialPermissionMode"] == "default"
    assert written["claudeCode.allowDangerouslySkipPermissions"] is False
    assert (settings_path, 0o600) in sftp.chmod_calls


def test_persistent_env_validates_names_and_values():
    assert persistent_env.normalize_env_updates({"HF_TOKEN": " hf_123 "}) == {"HF_TOKEN": "hf_123"}
    assert persistent_env.normalize_env_names(["HF_TOKEN", "HF_TOKEN"]) == ["HF_TOKEN"]
    assert "GOOGLE_DRIVE_REFRESH_TOKEN" in persistent_env.COMMON_ENV_NAMES
    assert persistent_env._uses_windows_expansion("%USERPROFILE%\\.cache\\huggingface")
    assert persistent_env._uses_windows_expansion("C:\\Users\\%USERNAME%\\tokens")
    assert persistent_env._uses_windows_expansion("%ProgramFiles(x86)%\\Google\\Cloud")
    assert not persistent_env._uses_windows_expansion("%USERPROFILE%\\secret", "HF_TOKEN")
    assert not persistent_env._uses_windows_expansion("token%with-percent")
    assert not persistent_env._uses_windows_expansion("100%")

    with pytest.raises(ValueError):
        persistent_env.normalize_env_updates({"BAD-NAME": "x"})

    with pytest.raises(ValueError):
        persistent_env.normalize_env_updates({"HF_TOKEN": "line\nbreak"})


def test_persistent_env_import_sources_include_existing_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_DRIVE_REFRESH_TOKEN", "drive-refresh-token")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "C:/Users/Zzy/google-service-account.json")

    sources = persistent_env.list_env_import_sources(include_profiles=False)
    source = next(item for item in sources if item.env_name == "GOOGLE_DRIVE_REFRESH_TOKEN")
    credentials = next(item for item in sources if item.env_name == "GOOGLE_APPLICATION_CREDENTIALS")

    assert source.label == "本机环境: GOOGLE_DRIVE_REFRESH_TOKEN"
    assert source.value == "drive-refresh-token"
    assert source.masked_value() == "drive-re...oken"
    assert source.preview_value() == "drive-re...oken"
    assert "GOOGLE_DRIVE_REFRESH_TOKEN=drive-re...oken" in source.display_label()
    assert credentials.preview_value() == "C:/Users/Zzy/google-service-account.json"


def test_persistent_env_import_sources_include_saved_api_profiles(isolated_ssh):
    security.set_secret("claude:relay:auth_token", "deepseek-key")
    security.set_secret("codex:kimi:api_key", "moonshot-key")
    profile_manager.save_claude_profile(
        ClaudeProfile(
            name="relay",
            auth_token_ref="claude:relay:auth_token",
            base_url="https://api.deepseek.com/anthropic",
            provider="deepseek",
        )
    )
    profile_manager.save_codex_profile(
        CodexProfile(
            name="kimi",
            api_key_ref="codex:kimi:api_key",
            model_provider="kimi",
        )
    )

    sources = persistent_env.list_env_import_sources(include_environment=False)
    values = {(source.label, source.env_name, source.value) for source in sources}

    assert ("Claude API: relay -> ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "deepseek-key") in values
    assert ("Claude API: relay -> DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY", "deepseek-key") in values
    assert ("Codex API: kimi -> MOONSHOT_API_KEY", "MOONSHOT_API_KEY", "moonshot-key") in values


def test_remote_user_env_writes_login_user_home_and_sources_existing_shells():
    sftp = _FakeSFTP()
    sftp.files["/home/test/.bashrc"] = b"# existing bashrc\n"
    client = _FakeClient(sftp, command_outputs=["/home/test"])

    result = persistent_env.set_remote_user_env(client, {"HF_TOKEN": "hf_test"})

    env_text = sftp.files["/home/test/.api_switcher_env"].decode("utf-8")
    profile_text = sftp.files["/home/test/.profile"].decode("utf-8")
    bashrc_text = sftp.files["/home/test/.bashrc"].decode("utf-8")

    assert result.env_file == "/home/test/.api_switcher_env"
    assert "export HF_TOKEN='hf_test'" in env_text
    assert persistent_env.REMOTE_SOURCE_BEGIN in profile_text
    assert persistent_env.REMOTE_SOURCE_BEGIN in bashrc_text
    assert "/home/test/.zshrc" not in sftp.files
    assert ("/home/test/.api_switcher_env", 0o600) in sftp.chmod_calls


def test_remote_user_env_upserts_without_dropping_existing_exports():
    sftp = _FakeSFTP()
    sftp.files["/home/test/.api_switcher_env"] = (
        b"# Managed by API\n"
        b"export OLD_TOKEN='old'\n"
        b"export HF_TOKEN='old_hf'\n"
    )
    client = _FakeClient(sftp, command_outputs=["/home/test"])

    persistent_env.set_remote_user_env(client, {"HF_TOKEN": "new'hf"})

    env_text = sftp.files["/home/test/.api_switcher_env"].decode("utf-8")
    assert "export OLD_TOKEN='old'" in env_text
    assert "export HF_TOKEN='new'\"'\"'hf'" in env_text
    assert "old_hf" not in env_text


def test_remote_user_env_delete_removes_only_selected_exports():
    sftp = _FakeSFTP()
    sftp.files["/home/test/.api_switcher_env"] = (
        b"# Managed by API\n"
        b"export OLD_TOKEN='old'\n"
        b"export HF_TOKEN='old_hf'\n"
        b"export OPENAI_API_KEY='sk-test'\n"
    )
    client = _FakeClient(sftp, command_outputs=["/home/test"])

    result = persistent_env.delete_remote_user_env(client, "HF_TOKEN")

    env_text = sftp.files["/home/test/.api_switcher_env"].decode("utf-8")
    assert result.summary() == "已删除 SSH 登录用户 /home/test: HF_TOKEN"
    assert "export OLD_TOKEN='old'" in env_text
    assert "export OPENAI_API_KEY='sk-test'" in env_text
    assert "HF_TOKEN" not in env_text


def _remote_codex_hook_test_paths():
    return remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )


def test_remote_codex_hooks_preserve_existing_entries(monkeypatch):
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    hooks_path = "/home/test/.codex/hooks.json"
    sftp.files[hooks_path] = json.dumps({
        "Stop": {"command": "sh /home/test/user_stop.sh", "timeout": 3},
        "Other": {"command": "sh /home/test/other.sh", "timeout": 2},
    }).encode("utf-8")
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path=hooks_path,
    )
    monkeypatch.setattr(remote_auto_continue, "_set_codex_hooks_enabled", lambda *args, **kwargs: None)

    remote_auto_continue._register_codex_hook(client, paths, "sh /home/test/.codex/hooks/auto_continue_stop.sh")
    hooks = json.loads(sftp.files[hooks_path].decode("utf-8"))
    stop_commands = list(remote_auto_continue._iter_codex_hook_commands(hooks, "Stop"))
    assert "sh /home/test/user_stop.sh" in stop_commands
    assert "sh /home/test/.codex/hooks/auto_continue_stop.sh" in stop_commands
    assert hooks["Other"]["command"] == "sh /home/test/other.sh"

    remote_auto_continue._unregister_codex_hook(client, paths)
    hooks = json.loads(sftp.files[hooks_path].decode("utf-8"))
    assert hooks["Stop"]["command"] == "sh /home/test/user_stop.sh"
    assert hooks["Other"]["command"] == "sh /home/test/other.sh"


def test_remote_codex_registers_error_recovery_hook(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    hooks_path = "/home/test/.codex/hooks.json"
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path=hooks_path,
    )
    monkeypatch.setattr(remote_auto_continue, "_set_codex_hooks_enabled", lambda *args, **kwargs: None)

    remote_auto_continue._register_codex_hook(
        client,
        paths,
        "sh /home/test/.codex/hooks/auto_continue_stop.sh",
        AutoContinueSettings(error_recovery_enabled=True),
    )

    hooks = json.loads(sftp.files[hooks_path].decode("utf-8"))
    assert "sh /home/test/.codex/hooks/auto_continue_stop.sh" in list(
        remote_auto_continue._iter_codex_hook_commands(hooks, "Stop")
    )
    assert "sh /home/test/.codex/hooks/auto_continue_stop.sh" in list(
        remote_auto_continue._iter_codex_hook_commands(hooks, "UserPromptSubmit")
    )
    assert "sh /home/test/.codex/hooks/auto_continue_stop.sh" in list(
        remote_auto_continue._iter_codex_hook_commands(hooks, "SessionStart")
    )
    assert "sh /home/test/.codex/hooks/auto_continue_stop.sh" in list(
        remote_auto_continue._iter_codex_hook_commands(hooks, "Error")
    )

    remote_auto_continue._unregister_codex_hook(client, paths)
    hooks = json.loads(sftp.files[hooks_path].decode("utf-8"))
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "Stop"))
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "UserPromptSubmit"))
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "SessionStart"))
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "Error"))


def test_remote_codex_hook_repair_backs_up_invalid_hooks_json(monkeypatch):
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    hooks_path = "/home/test/.codex/hooks.json"
    sftp.files[hooks_path] = b"{not valid json"
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path=hooks_path,
    )
    monkeypatch.setattr(remote_auto_continue, "_set_codex_hooks_enabled", lambda *args, **kwargs: None)

    remote_auto_continue._register_codex_hook(client, paths, "sh /home/test/.codex/hooks/auto_continue_stop.sh")

    backups = [path for path in sftp.files if path.startswith(hooks_path + ".bak-")]
    assert len(backups) == 1
    assert sftp.files[backups[0]] == b"{not valid json"
    hooks = json.loads(sftp.files[hooks_path].decode("utf-8"))
    assert "sh /home/test/.codex/hooks/auto_continue_stop.sh" in list(
        remote_auto_continue._iter_codex_hook_commands(hooks, "Stop")
    )


def test_remote_pause_treats_string_false_feature_flags_as_disabled(monkeypatch):
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    sftp.files[paths.settings_path] = json.dumps({
        "enabled": "true",
        "git_auto_snapshot": "false",
        "git_snapshot_on_start": "true",
        "error_recovery_enabled": "false",
        "auto_approve_permission_requests": "false",
    }).encode("utf-8")
    sftp.files[paths.provider_config_path] = b"[features]\ncodex_hooks = true\n"
    sftp.files[paths.codex_hooks_path] = json.dumps({
        "hooks": {
            "Stop": [{
                "hooks": [{
                    "command": "sh /home/test/.codex/hooks/auto_continue_stop.sh",
                }],
            }],
        },
    }).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    remote_auto_continue.pause_remote_auto_continue("remote", "codex")

    settings = json.loads(sftp.files[paths.settings_path].decode("utf-8"))
    hooks = json.loads(sftp.files[paths.codex_hooks_path].decode("utf-8"))

    assert settings["enabled"] is False
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "Stop"))


def test_remote_codex_hooks_feature_prefers_features_section():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    remote_auto_continue._set_codex_hooks_enabled(client, paths, True)
    config = tomllib.loads(sftp.files[paths.provider_config_path].decode("utf-8"))
    assert config["features"]["hooks"] is True
    assert "codex_hooks" not in config["features"]
    assert "codex_hooks" not in config

    remote_auto_continue._set_codex_hooks_enabled(client, paths, False)
    config = tomllib.loads(sftp.files[paths.provider_config_path].decode("utf-8"))
    assert config["features"]["hooks"] is False


def test_remote_codex_hooks_feature_syncs_legacy_root_flag():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    sftp.files[paths.provider_config_path] = b"codex_hooks = false\n"

    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    remote_auto_continue._set_codex_hooks_enabled(client, paths, True)
    config = tomllib.loads(sftp.files[paths.provider_config_path].decode("utf-8"))
    assert config["codex_hooks"] is True
    assert config["features"]["hooks"] is True
    assert remote_auto_continue._codex_hooks_enabled_from_config(config) is True

    remote_auto_continue._set_codex_hooks_enabled(client, paths, False)
    config = tomllib.loads(sftp.files[paths.provider_config_path].decode("utf-8"))
    assert config["codex_hooks"] is False
    assert config["features"]["hooks"] is False
    assert remote_auto_continue._codex_hooks_enabled_from_config(config) is False
    assert remote_auto_continue._codex_hooks_enabled_from_config({"codex_hooks": True}) is True
    assert remote_auto_continue._codex_hooks_enabled_from_config({
        "codex_hooks": True,
        "features": {"codex_hooks": False},
    }) is False


def test_remote_codex_hook_round_trip_preserves_user_group_metadata():
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = _remote_codex_hook_test_paths()
    user_group = {
        "matcher": "tool == 'shell'",
        "custom": {"owner": "user", "priority": 7},
        "hooks": [{"type": "command", "command": "sh /home/test/user.sh", "timeout": 4}],
    }
    sftp.files[paths.codex_hooks_path] = json.dumps({
        "hooks": {"Stop": [user_group]},
        "userMetadata": {"keep": True},
    }).encode("utf-8")
    sftp.files[paths.provider_config_path] = (
        b"# keep this comment\n[features]\nhooks = false # user value\n\n[model]\nname = 'x'\n"
    )

    remote_auto_continue._register_codex_hook(
        client,
        paths,
        "sh /home/test/.codex/hooks/auto_continue_stop.sh",
        AutoContinueSettings(enabled=True, git_auto_snapshot=False),
    )
    installed = json.loads(sftp.files[paths.codex_hooks_path].decode("utf-8"))
    assert installed["hooks"]["Stop"][0] == user_group
    assert installed["userMetadata"] == {"keep": True}

    remote_auto_continue._unregister_codex_hook(client, paths)
    restored = json.loads(sftp.files[paths.codex_hooks_path].decode("utf-8"))
    assert restored["hooks"]["Stop"] == [user_group]
    assert restored["userMetadata"] == {"keep": True}
    config_text = sftp.files[paths.provider_config_path].decode("utf-8")
    assert "# keep this comment" in config_text
    assert "[model]" in config_text
    assert remote_auto_continue._read_toml(client, paths.provider_config_path)["features"]["hooks"] is True
    assert remote_auto_continue._codex_hooks_feature_state_path(paths) not in sftp.files


@pytest.mark.parametrize("original_enabled", [False, True])
def test_remote_codex_hook_uninstall_restores_owned_feature_state(original_enabled):
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = _remote_codex_hook_test_paths()
    value = "true" if original_enabled else "false"
    sftp.files[paths.provider_config_path] = (
        f"# preserve\n[features]\nhooks = {value} # original\n".encode("utf-8")
    )

    remote_auto_continue._register_codex_hook(
        client,
        paths,
        "sh /home/test/.codex/hooks/auto_continue_stop.sh",
    )
    assert remote_auto_continue._read_toml(client, paths.provider_config_path)["features"]["hooks"] is True

    remote_auto_continue._unregister_codex_hook(client, paths)
    config = remote_auto_continue._read_toml(client, paths.provider_config_path)
    assert config["features"]["hooks"] is original_enabled
    assert "# preserve" in sftp.files[paths.provider_config_path].decode("utf-8")
    assert remote_auto_continue._codex_hooks_feature_state_path(paths) not in sftp.files


def test_remote_codex_inline_features_are_updated_without_rewriting_other_config():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = _remote_codex_hook_test_paths()
    sftp.files[paths.provider_config_path] = (
        b"features = { telemetry = false } # inline\nmodel = 'gpt-test'\n"
    )

    remote_auto_continue._set_codex_hooks_enabled(client, paths, True)

    text = sftp.files[paths.provider_config_path].decode("utf-8")
    config = remote_auto_continue._read_toml(client, paths.provider_config_path)
    assert config["features"] == {"telemetry": False, "hooks": True}
    assert "# inline" in text
    assert "model = 'gpt-test'" in text


def test_remote_codex_invalid_toml_rolls_back_hooks_and_creates_backup():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = _remote_codex_hook_test_paths()
    original_hooks = json.dumps({
        "hooks": {"Stop": [{"hooks": [{"command": "sh /home/test/user.sh"}]}]},
    }).encode("utf-8")
    invalid_config = b"[features\nhooks = true\n"
    sftp.files[paths.codex_hooks_path] = original_hooks
    sftp.files[paths.provider_config_path] = invalid_config

    with pytest.raises(RuntimeError, match="config.toml"):
        remote_auto_continue._register_codex_hook(
            client,
            paths,
            "sh /home/test/.codex/hooks/auto_continue_stop.sh",
        )

    assert sftp.files[paths.codex_hooks_path] == original_hooks
    assert sftp.files[paths.provider_config_path] == invalid_config
    assert remote_auto_continue._codex_hooks_feature_state_path(paths) not in sftp.files
    backups = [path for path in sftp.files if path.startswith(paths.provider_config_path + ".bak-")]
    assert len(backups) == 1
    assert sftp.files[backups[0]] == invalid_config


def test_remote_git_snapshot_settings_do_not_inherit_error_or_permission_hooks():
    from models.auto_continue import AutoContinueSettings

    settings = AutoContinueSettings(
        enabled=True,
        error_recovery_enabled=True,
        auto_approve_permission_requests=True,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )

    claude = remote_auto_continue._load_git_snapshot_settings("claude", settings)
    codex = remote_auto_continue._load_git_snapshot_settings("codex", settings)

    for resolved in [claude, codex]:
        assert resolved.enabled is False
        assert resolved.git_auto_snapshot is True
        assert resolved.git_snapshot_on_start is True
        assert resolved.error_recovery_enabled is False
        assert resolved.auto_approve_permission_requests is False


def test_remote_hook_requirement_covers_error_recovery_and_permission():
    from models.auto_continue import AutoContinueSettings

    assert remote_auto_continue._settings_require_remote_hook(
        "codex",
        AutoContinueSettings(enabled=False, git_auto_snapshot=False, error_recovery_enabled=True),
    )
    assert remote_auto_continue._settings_require_remote_hook(
        "claude",
        AutoContinueSettings(
            enabled=False,
            git_auto_snapshot=False,
            auto_approve_permission_requests=True,
        ),
    )
    assert not remote_auto_continue._settings_require_remote_hook(
        "codex",
        AutoContinueSettings(
            enabled=False,
            git_auto_snapshot=False,
            auto_approve_permission_requests=True,
        ),
    )


def test_update_remote_codex_error_recovery_switch_registers_error_hook(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    sftp.files[paths.settings_path] = json.dumps(
        AutoContinueSettings(
            enabled=False,
            git_auto_snapshot=False,
            git_snapshot_on_start=False,
            error_recovery_enabled=False,
        ).to_dict()
    ).encode("utf-8")
    runtime_checks = []

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)
    monkeypatch.setattr(
        remote_auto_continue,
        "_ensure_remote_runtime",
        lambda _client, _profile, require_git=False: runtime_checks.append(require_git) or {},
    )

    remote_auto_continue.update_remote_auto_continue_settings(
        "remote",
        "codex",
        {"error_recovery_enabled": True},
    )

    settings = json.loads(sftp.files[paths.settings_path].decode("utf-8"))
    hooks = json.loads(sftp.files[paths.codex_hooks_path].decode("utf-8"))

    assert settings["error_recovery_enabled"] is True
    assert runtime_checks == [False]
    assert sftp.files[paths.script_path].startswith(b"#!/bin/sh")
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "Error"))
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "Stop"))


def test_update_remote_switch_without_remote_settings_keeps_git_snapshot_on_by_default(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    local_settings = AutoContinueSettings(
        enabled=True,
        git_auto_snapshot=True,
        git_snapshot_on_start=True,
        error_recovery_enabled=False,
    )
    runtime_checks = []

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)
    monkeypatch.setattr(remote_auto_continue.auto_continue_manager, "get_settings", lambda _provider: local_settings)
    monkeypatch.setattr(
        remote_auto_continue,
        "_ensure_remote_runtime",
        lambda _client, _profile, require_git=False: runtime_checks.append(require_git) or {},
    )

    remote_auto_continue.update_remote_auto_continue_settings(
        "remote",
        "codex",
        {"error_recovery_enabled": True},
    )

    settings = json.loads(sftp.files[paths.settings_path].decode("utf-8"))
    hooks = json.loads(sftp.files[paths.codex_hooks_path].decode("utf-8"))

    assert settings["enabled"] is False
    assert settings["git_auto_snapshot"] is True
    assert settings["git_snapshot_on_start"] is True
    assert settings["error_recovery_enabled"] is True
    assert runtime_checks == [True]
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "Stop"))
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "UserPromptSubmit"))
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "SessionStart"))
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "Error"))


def test_update_remote_training_switch_uses_local_training_prompt(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    local_settings = AutoContinueSettings(
        enabled=True,
        training_auto_continue_enabled=False,
        training_prompt_template_key="classification",
        training_continue_prompt="val_acc >= 0.95",
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        error_recovery_enabled=False,
    )

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)
    monkeypatch.setattr(remote_auto_continue.auto_continue_manager, "get_settings", lambda _provider: local_settings)
    monkeypatch.setattr(remote_auto_continue, "_ensure_remote_runtime", lambda *_args, **_kwargs: {})

    remote_auto_continue.update_remote_auto_continue_settings(
        "remote",
        "codex",
        {"training_auto_continue_enabled": True},
    )

    settings = json.loads(sftp.files[paths.settings_path].decode("utf-8"))
    hooks = json.loads(sftp.files[paths.codex_hooks_path].decode("utf-8"))

    assert settings["enabled"] is False
    assert settings["training_auto_continue_enabled"] is True
    assert settings["training_prompt_template_key"] == "classification"
    assert settings["training_continue_prompt"] == "val_acc >= 0.95"
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "Stop"))
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "Error"))


def test_remote_claude_permission_only_registers_permission_hooks_without_stop():
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    settings_path = "/home/test/.claude/settings.json"
    permission_rules_path = "/home/test/.claude/auto_continue_permission_rules.json"
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path=settings_path,
        permission_rules_path=permission_rules_path,
    )

    remote_auto_continue._register_claude_hook(
        client,
        paths,
        "sh /home/test/.claude/hooks/auto_continue_stop.sh",
        True,
        AutoContinueSettings(
            enabled=False,
            git_auto_snapshot=False,
            git_snapshot_on_start=False,
            auto_approve_permission_requests=True,
        ),
    )

    settings = json.loads(sftp.files[settings_path].decode("utf-8"))

    assert not list(remote_auto_continue._iter_claude_hook_commands(settings, ("Stop",)))
    assert not list(remote_auto_continue._iter_claude_hook_commands(settings, ("SubagentStop",)))
    assert not list(remote_auto_continue._iter_claude_hook_commands(settings, ("UserPromptSubmit",)))
    assert not list(remote_auto_continue._iter_claude_hook_commands(settings, ("SessionStart",)))
    assert list(remote_auto_continue._iter_claude_hook_commands(settings, ("PreToolUse",)))
    assert list(remote_auto_continue._iter_claude_hook_commands(settings, ("PermissionRequest",)))


def test_remote_training_guard_registers_chain_reset_hooks_without_general_auto_continue():
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )

    remote_auto_continue._register_codex_hook(
        client,
        paths,
        "sh /home/test/.codex/hooks/auto_continue_stop.sh",
        AutoContinueSettings(
            enabled=False,
            training_auto_continue_enabled=True,
            git_auto_snapshot=False,
            git_snapshot_on_start=False,
        ),
    )

    hooks = json.loads(sftp.files[paths.codex_hooks_path].decode("utf-8"))
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "Stop"))
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "UserPromptSubmit"))
    assert list(remote_auto_continue._iter_codex_hook_commands(hooks, "SessionStart"))
    assert not list(remote_auto_continue._iter_codex_hook_commands(hooks, "Error"))


def test_update_remote_codex_permission_switch_is_ignored(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    sftp.files[paths.settings_path] = json.dumps(
        AutoContinueSettings(
            enabled=False,
            git_auto_snapshot=False,
            git_snapshot_on_start=False,
        ).to_dict()
    ).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    remote_auto_continue.update_remote_auto_continue_settings(
        "remote",
        "codex",
        {"auto_approve_permission_requests": True},
    )

    settings = json.loads(sftp.files[paths.settings_path].decode("utf-8"))
    assert settings["auto_approve_permission_requests"] is False
    assert paths.codex_hooks_path not in sftp.files


def test_remote_claude_auto_approve_preseeds_permission_allow_rules():
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    settings_path = "/home/test/.claude/settings.json"
    permission_rules_path = "/home/test/.claude/auto_continue_permission_rules.json"
    sftp.files[settings_path] = json.dumps({
        "permissions": {
            "allow": ["Read(/tmp/**)", "Edit"],
            "ask": ["Read", "Bash", "Write"],
        },
        "hooks": {"Stop": [{"hooks": [{"command": "sh /home/test/user_stop.sh"}]}]},
    }).encode("utf-8")
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path=settings_path,
        permission_rules_path=permission_rules_path,
    )

    remote_auto_continue._register_claude_hook(
        client,
        paths,
        "sh /home/test/.claude/hooks/auto_continue_stop.sh",
        False,
        AutoContinueSettings(
            auto_approve_permission_requests=True,
            auto_approve_tools=["Bash", "Edit", "Write"],
        ),
    )

    settings = json.loads(sftp.files[settings_path].decode("utf-8"))
    assert "PreToolUse" in settings["hooks"]
    assert "PermissionRequest" in settings["hooks"]
    assert settings["permissions"]["defaultMode"] == "dontAsk"
    assert settings["skipDangerousModePermissionPrompt"] is False
    assert settings["permissions"]["allow"] == ["Read(/tmp/**)", "Edit", "Bash", "Write"]
    assert settings["permissions"]["ask"] == ["Read"]
    state = json.loads(sftp.files[permission_rules_path].decode("utf-8"))
    assert state["rules"] == ["Bash", "Write"]
    assert state["ask_rules"] == ["Bash", "Write"]

    remote_auto_continue._unregister_claude_hook(client, paths)

    settings = json.loads(sftp.files[settings_path].decode("utf-8"))
    assert settings["permissions"]["allow"] == ["Read(/tmp/**)", "Edit"]
    assert settings["permissions"]["ask"] == ["Read", "Bash", "Write"]
    assert permission_rules_path not in sftp.files


def test_remote_claude_registers_response_error_hook():
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    settings_path = "/home/test/.claude/settings.json"
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path=settings_path,
        permission_rules_path="/home/test/.claude/auto_continue_permission_rules.json",
    )

    remote_auto_continue._register_claude_hook(
        client,
        paths,
        "sh /home/test/.claude/hooks/auto_continue_stop.sh",
        False,
        AutoContinueSettings(error_recovery_enabled=True),
    )

    settings = json.loads(sftp.files[settings_path].decode("utf-8"))
    prompt_commands = list(remote_auto_continue._iter_claude_hook_commands(settings, ("UserPromptSubmit",)))
    session_commands = list(remote_auto_continue._iter_claude_hook_commands(settings, ("SessionStart",)))
    response_error_commands = list(remote_auto_continue._iter_claude_hook_commands(settings, ("ResponseError",)))
    assert "sh /home/test/.claude/hooks/auto_continue_stop.sh" in prompt_commands
    assert "sh /home/test/.claude/hooks/auto_continue_stop.sh" in session_commands
    assert "sh /home/test/.claude/hooks/auto_continue_stop.sh" in response_error_commands

    remote_auto_continue._unregister_claude_hook(client, paths)
    settings = json.loads(sftp.files[settings_path].decode("utf-8"))
    assert not list(remote_auto_continue._iter_claude_hook_commands(settings, ("UserPromptSubmit",)))
    assert not list(remote_auto_continue._iter_claude_hook_commands(settings, ("SessionStart",)))
    assert not list(remote_auto_continue._iter_claude_hook_commands(settings, ("ResponseError",)))


def test_remote_claude_hook_repair_backs_up_invalid_settings_json():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    settings_path = "/home/test/.claude/settings.json"
    sftp.files[settings_path] = b"{not valid json"
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path=settings_path,
        permission_rules_path="/home/test/.claude/auto_continue_permission_rules.json",
    )

    remote_auto_continue._register_claude_hook(
        client,
        paths,
        "sh /home/test/.claude/hooks/auto_continue_stop.sh",
        False,
    )

    backups = [path for path in sftp.files if path.startswith(settings_path + ".bak-")]
    assert len(backups) == 1
    assert sftp.files[backups[0]] == b"{not valid json"
    settings = json.loads(sftp.files[settings_path].decode("utf-8"))
    assert "sh /home/test/.claude/hooks/auto_continue_stop.sh" in list(
        remote_auto_continue._iter_claude_hook_commands(settings, ("Stop",))
    )


def test_remote_claude_unregister_cleans_permission_sidecar_without_settings():
    sftp = _FakeSFTP()
    permission_rules_path = "/home/test/.claude/auto_continue_permission_rules.json"
    sftp.files[permission_rules_path] = json.dumps({"rules": ["Bash"], "ask_rules": ["Bash"]}).encode("utf-8")
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path="/home/test/.claude/settings.json",
        permission_rules_path=permission_rules_path,
    )

    remote_auto_continue._unregister_claude_hook(client, paths)

    assert permission_rules_path not in sftp.files


def test_remote_claude_status_flags_prompting_permission_mode(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path="/home/test/.claude/settings.json",
        permission_rules_path="/home/test/.claude/auto_continue_permission_rules.json",
    )
    auto_settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        auto_approve_permission_requests=True,
        auto_approve_tools=["Bash"],
    )
    sftp.files[paths.script_path] = _expected_remote_hook_script(paths)
    sftp.files[paths.settings_path] = json.dumps(auto_settings.to_dict()).encode("utf-8")
    sftp.files[paths.guidance_path] = b"BEGIN AUTO CONTINUE GUIDANCE\n"
    sftp.files[paths.provider_config_path] = json.dumps({
        "permissions": {"defaultMode": "acceptEdits", "allow": ["Bash"]},
        "hooks": {
            "PreToolUse": [{"hooks": [{"command": "sh /home/test/.claude/hooks/auto_continue_stop.sh"}]}],
            "PermissionRequest": [{"hooks": [{"command": "sh /home/test/.claude/hooks/auto_continue_stop.sh"}]}],
        },
    }).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "/usr/bin/git",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "claude")

    assert status.permission_mode == "acceptEdits"
    assert not status.ready
    assert any("dontAsk" in issue for issue in status.issues)

    sftp.files[paths.provider_config_path] = json.dumps({
        "permissions": {"defaultMode": "dontAsk", "allow": ["Bash"]},
        "hooks": {
            "PreToolUse": [{"hooks": [{"command": "sh /home/test/.claude/hooks/auto_continue_stop.sh"}]}],
            "PermissionRequest": [{"hooks": [{"command": "sh /home/test/.claude/hooks/auto_continue_stop.sh"}]}],
        },
    }).encode("utf-8")

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "claude")

    assert status.permission_mode == "dontAsk"
    assert status.ready


def test_remote_claude_status_requires_permission_hooks_and_reports_wildcard_deny(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path="/home/test/.claude/settings.json",
        permission_rules_path="/home/test/.claude/auto_continue_permission_rules.json",
    )
    auto_settings = AutoContinueSettings(
        enabled=False,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
        auto_approve_permission_requests=True,
        auto_approve_bash=False,
        auto_approve_tools=["*"],
    )
    sftp.files[paths.script_path] = b"#!/bin/sh\n"
    sftp.files[paths.settings_path] = json.dumps(auto_settings.to_dict()).encode("utf-8")
    sftp.files[paths.guidance_path] = b"BEGIN AUTO CONTINUE GUIDANCE\n"
    sftp.files[paths.provider_config_path] = json.dumps({
        "permissions": {"defaultMode": "dontAsk", "deny": ["Edit"]},
        "hooks": {"Stop": [{"hooks": [{"command": "sh /home/test/.claude/hooks/auto_continue_stop.sh"}]}]},
    }).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "/usr/bin/git",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "claude")

    assert not status.ready
    assert any("PreToolUse" in issue and "PermissionRequest" in issue for issue in status.issues)
    assert any("通配" in issue and "Edit" in issue for issue in status.issues)


def test_remote_git_snapshot_status_ready_without_auto_continue():
    status = remote_auto_continue.RemoteAutoContinueStatus(
        provider_name="codex",
        enabled=False,
        git_snapshot_enabled=True,
        git_available=True,
        hook_script_exists=True,
        hook_registered=True,
        settings_valid=True,
        runtime_ready=True,
        codex_hooks_enabled=True,
    )

    assert status.ready


def test_remote_training_guard_status_ready_without_general_auto_continue():
    status = remote_auto_continue.RemoteAutoContinueStatus(
        provider_name="codex",
        enabled=False,
        training_auto_continue_enabled=True,
        git_snapshot_enabled=False,
        git_available=False,
        hook_script_exists=True,
        hook_registered=True,
        settings_valid=True,
        runtime_ready=True,
        codex_hooks_enabled=True,
    )

    assert status.ready


def test_remote_status_with_diagnostics_is_not_ready():
    status = remote_auto_continue.RemoteAutoContinueStatus(
        provider_name="claude",
        enabled=True,
        hook_script_exists=True,
        hook_registered=True,
        settings_valid=True,
        runtime_ready=True,
        issues=["permissions.ask 仍会强制询问: Bash"],
    )

    assert not status.ready


def test_remote_git_snapshot_status_requires_git():
    status = remote_auto_continue.RemoteAutoContinueStatus(
        provider_name="codex",
        enabled=False,
        git_snapshot_enabled=True,
        git_available=False,
        hook_script_exists=True,
        hook_registered=True,
        settings_valid=True,
        runtime_ready=True,
        codex_hooks_enabled=True,
    )

    assert not status.ready


def test_remote_auto_status_summary_clarifies_git_push_scope():
    status = remote_auto_continue.RemoteAutoContinueStatus(
        provider_name="codex",
        enabled=False,
        git_snapshot_enabled=True,
        git_snapshot_master_enabled=True,
        git_snapshot_on_start_enabled=True,
        git_snapshot_on_recovery_enabled=True,
        git_auto_push_enabled=True,
        git_available=True,
        hook_script_exists=True,
        hook_script_matches_expected=True,
        hook_registered=True,
        settings_valid=True,
        settings_matches_expected=True,
        runtime_ready=True,
        codex_hooks_enabled=True,
    )

    summary = status.summary()

    assert "Git快照 ON" in summary
    assert "推送已有 Git remote ON" in summary
    assert "Git push" not in summary


def test_remote_status_requires_error_hook_for_enabled_error_recovery(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    settings = AutoContinueSettings(
        enabled=False,
        error_recovery_enabled=True,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    sftp.files[paths.script_path] = b"#!/bin/sh\n"
    sftp.files[paths.settings_path] = json.dumps(settings.to_dict()).encode("utf-8")
    sftp.files[paths.provider_config_path] = b"[features]\ncodex_hooks = true\n"
    sftp.files[paths.codex_hooks_path] = json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"command": "sh /home/test/.codex/hooks/auto_continue_stop.sh"}]}]
        }
    }).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "/usr/bin/git",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "codex")

    assert not status.ready
    assert status.error_recovery_enabled is True
    assert any("Error Hook" in issue for issue in status.issues)


def test_remote_codex_status_reports_invalid_hooks_json(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    settings = AutoContinueSettings(enabled=True, git_auto_snapshot=False)
    sftp.files[paths.script_path] = b"#!/bin/sh\n"
    sftp.files[paths.settings_path] = json.dumps(settings.to_dict()).encode("utf-8")
    sftp.files[paths.provider_config_path] = b"[features]\ncodex_hooks = true\n"
    sftp.files[paths.codex_hooks_path] = b"{not valid json"

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "/usr/bin/git",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "codex")

    assert not status.ready
    assert any("hooks.json" in issue for issue in status.issues)


def test_remote_codex_status_flags_stale_hook_script(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    settings = AutoContinueSettings(
        enabled=True,
        git_auto_snapshot=False,
        git_snapshot_on_start=False,
    )
    sftp.files[paths.script_path] = b"#!/bin/sh\n# stale\n"
    sftp.files[paths.settings_path] = json.dumps(settings.to_dict()).encode("utf-8")
    sftp.files[paths.guidance_path] = b"BEGIN AUTO CONTINUE GUIDANCE\n"
    sftp.files[paths.provider_config_path] = b"[features]\ncodex_hooks = true\n"
    sftp.files[paths.codex_hooks_path] = json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"command": "sh /home/test/.codex/hooks/auto_continue_stop.sh"}]}]
        }
    }).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "/usr/bin/git",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "codex")

    assert status.hook_script_matches_expected is False
    assert status.hook_script_sha256 != status.expected_hook_script_sha256
    assert not status.ready
    assert any("不一致" in issue for issue in status.issues)


def test_remote_codex_status_flags_stale_settings_schema(monkeypatch):
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    sftp.files[paths.script_path] = _expected_remote_hook_script(paths)
    sftp.files[paths.settings_path] = json.dumps({
        "enabled": True,
        "git_auto_snapshot": False,
    }).encode("utf-8")
    sftp.files[paths.guidance_path] = b"BEGIN AUTO CONTINUE GUIDANCE\n"
    sftp.files[paths.provider_config_path] = b"[features]\ncodex_hooks = true\n"
    sftp.files[paths.codex_hooks_path] = json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"command": "sh /home/test/.codex/hooks/auto_continue_stop.sh"}]}]
        }
    }).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "/usr/bin/git",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "codex")

    assert status.settings_matches_expected is False
    assert not status.ready
    assert any("设置" in issue and "一键修复" in issue for issue in status.issues)


def test_remote_status_requires_git_for_error_recovery_snapshot(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="codex",
        config_dir="/home/test/.codex",
        hooks_dir="/home/test/.codex/hooks",
        settings_path="/home/test/.codex/auto_continue_settings.json",
        script_path="/home/test/.codex/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.codex/tmp",
        guidance_path="/home/test/.codex/AGENTS.md",
        provider_config_path="/home/test/.codex/config.toml",
        permission_rules_path="/home/test/.codex/auto_continue_permission_rules.json",
        codex_hooks_path="/home/test/.codex/hooks.json",
    )
    settings = AutoContinueSettings(
        enabled=False,
        error_recovery_enabled=True,
        git_auto_snapshot=True,
        git_snapshot_on_start=False,
        git_snapshot_on_recovery=True,
    )
    sftp.files[paths.script_path] = b"#!/bin/sh\n"
    sftp.files[paths.settings_path] = json.dumps(settings.to_dict()).encode("utf-8")
    sftp.files[paths.provider_config_path] = b"[features]\ncodex_hooks = true\n"
    sftp.files[paths.codex_hooks_path] = json.dumps({
        "hooks": {
            "Error": [{"hooks": [{"command": "sh /home/test/.codex/hooks/auto_continue_stop.sh"}]}]
        }
    }).encode("utf-8")

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "codex")

    assert not status.ready
    assert any("git" in issue for issue in status.issues)


def test_remote_claude_status_reports_invalid_settings_json(monkeypatch):
    from models.auto_continue import AutoContinueSettings

    sftp = _FakeSFTP()
    client = _FakeClient(sftp)
    paths = remote_auto_continue.RemoteAutoContinuePaths(
        provider_name="claude",
        config_dir="/home/test/.claude",
        hooks_dir="/home/test/.claude/hooks",
        settings_path="/home/test/.claude/auto_continue_settings.json",
        script_path="/home/test/.claude/hooks/auto_continue_stop.sh",
        state_dir="/home/test/.claude/tmp",
        guidance_path="/home/test/.claude/CLAUDE.md",
        provider_config_path="/home/test/.claude/settings.json",
        permission_rules_path="/home/test/.claude/auto_continue_permission_rules.json",
    )
    settings = AutoContinueSettings(enabled=True, git_auto_snapshot=False)
    sftp.files[paths.script_path] = b"#!/bin/sh\n"
    sftp.files[paths.settings_path] = json.dumps(settings.to_dict()).encode("utf-8")
    sftp.files[paths.provider_config_path] = b"{not valid json"

    monkeypatch.setattr(remote_auto_continue, "_connect", lambda ssh_name: (SSHProfile(name="remote", host="host"), client))
    monkeypatch.setattr(
        remote_auto_continue,
        "_probe_remote_environment",
        lambda _client: {
            "os": "Linux",
            "sh": "/bin/sh",
            "python": "/usr/bin/python3",
            "git": "/usr/bin/git",
            "is_posix": True,
        },
    )
    monkeypatch.setattr(remote_auto_continue, "_paths", lambda _client, _profile, _provider: paths)

    status = remote_auto_continue.get_remote_auto_continue_status("remote", "claude")

    assert not status.ready
    assert any("settings.json" in issue for issue in status.issues)


def test_remote_dependency_install_commands():
    assert (
        remote_auto_continue._install_command_for_packages("apt-get", ["git", "python"])
        == "DEBIAN_FRONTEND=noninteractive apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y git python3"
    )
    assert remote_auto_continue._install_command_for_packages("pacman", ["python"]) == "pacman -Sy --noconfirm python"
    assert remote_auto_continue._install_command_for_packages("apk", ["git"]) == "apk add --no-cache git"


def test_remote_hook_script_contains_compilable_error_recovery_python():
    script = remote_auto_continue._generate_remote_hook_script(
        "/home/test/.codex/auto_continue_settings.json",
        "/home/test/.codex/tmp",
    )
    assert "handle_error_recovery" in script
    assert "error_recovery_state.json" in script
    assert "Retry-After" in script
    assert '"Error"' in script
    assert "__CONTENT_LENGTH_PATTERNS__" not in script

    start = script.index("<<'PY'") + len("<<'PY'")
    start = script.index("\n", start) + 1
    end = script.index("\nPY\n", start)
    compile(script[start:end], "<remote_auto_continue_hook>", "exec")
