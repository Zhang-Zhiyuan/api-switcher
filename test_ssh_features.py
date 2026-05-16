import json
from io import BytesIO

import pytest

from core import persistent_env, profile_manager, remote_auto_continue, remote_config, security, sync_manager
from core.ssh_manager import SSHManager, ssh_manager
from core.ssh_profile_builder import build_ssh_profile_from_data
from models.profile import ClaudeAccountProfile, ClaudeProfile, CodexAccountProfile, CodexProfile, SSHProfile


@pytest.fixture()
def isolated_ssh(tmp_path, monkeypatch):
    secret_store: dict[str, str] = {}

    monkeypatch.setattr(security, "set_secret", lambda key, value: secret_store.__setitem__(key, value or ""))
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(security, "set_secret_json", lambda key, data: secret_store.__setitem__(key, json.dumps(data)))
    monkeypatch.setattr(security, "get_secret_json", lambda key: json.loads(secret_store[key]) if key in secret_store else None)
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")

    return secret_store


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
    written = {}
    fake_client = object()

    def fake_connect(profile):
        connected["profile"] = profile
        return fake_client

    monkeypatch.setattr(sync_manager.ssh_manager, "connect", fake_connect)
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {"auth_mode": "chatgpt", "tokens": {"id": "old"}})
    monkeypatch.setattr(remote_config, "write_remote_codex_config", lambda client, data, profile=None: written.setdefault("config", (client, data, profile)))
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", lambda client, data, profile=None: written.setdefault("auth", (client, data, profile)))
    monkeypatch.setattr(persistent_env, "set_remote_user_env", lambda client, data: written.setdefault("env", (client, data)))

    message = sync_manager.sync_codex_to_server("remote", "relay")

    assert connected["profile"].name == "remote"
    assert written["config"][0] is fake_client
    assert written["auth"][1]["auth_mode"] == "apikey"
    assert written["auth"][1]["OPENAI_API_KEY"] == "sk-relay"
    assert written["auth"][2].name == "remote"
    assert written["env"] == (fake_client, {"OPENAI_API_KEY": "sk-relay"})
    assert "OPENAI_API_KEY" in message
    assert "ssh.example.com" in message


def test_sync_codex_to_server_writes_openai_key_fallback_for_provider_env(isolated_ssh, monkeypatch):
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

    written = {}
    fake_client = object()

    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "write_remote_codex_config", lambda client, data, profile=None: None)
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", lambda client, data, profile=None: None)
    monkeypatch.setattr(persistent_env, "set_remote_user_env", lambda client, data: written.setdefault("env", data))

    message = sync_manager.sync_codex_to_server("remote", "deepseek")

    assert written["env"] == {
        "DEEPSEEK_API_KEY": "sk-deepseek",
        "OPENAI_API_KEY": "sk-deepseek",
    }
    assert "DEEPSEEK_API_KEY" in message
    assert "OPENAI_API_KEY" in message


def test_sync_codex_to_server_applies_remote_wire_api_benchmark(isolated_ssh, monkeypatch):
    security.set_secret("codex:layer4:api_key", "sk-layer4")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="layer4",
            api_key_ref="codex:layer4:api_key",
            model="gpt-5.5",
            model_provider="layer4",
            custom_base_url="https://layer4.example.com/v1",
            custom_wire_api="responses",
        )
    )

    fake_client = object()
    writes = []
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "write_remote_codex_config", lambda client, data, profile=None: writes.append(json.loads(json.dumps(data))))
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", lambda client, data, profile=None: None)
    monkeypatch.setattr(persistent_env, "set_remote_user_env", lambda client, data: None)
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

    message = sync_manager.sync_codex_to_server("remote", "layer4")

    assert len(writes) == 1
    assert writes[0]["model_providers"]["layer4"]["wire_api"] == "responses"
    assert "wire_api=responses" in message
    assert "responses 3/3" in message


def test_sync_codex_to_server_can_force_wire_api_without_benchmark(isolated_ssh, monkeypatch):
    security.set_secret("codex:layer4:api_key", "sk-layer4")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="layer4",
            api_key_ref="codex:layer4:api_key",
            model="gpt-5.5",
            model_provider="layer4",
            custom_base_url="https://layer4.example.com/v1",
            custom_wire_api="responses",
        )
    )

    fake_client = object()
    writes = []
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "write_remote_codex_config", lambda client, data, profile=None: writes.append(json.loads(json.dumps(data))))
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", lambda client, data, profile=None: None)
    monkeypatch.setattr(persistent_env, "set_remote_user_env", lambda client, data: None)
    monkeypatch.setattr(
        sync_manager,
        "_remote_benchmark_codex_wire_api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("manual wire_api must not benchmark")),
    )

    message = sync_manager.sync_codex_to_server("remote", "layer4", wire_api_mode="chat")

    assert len(writes) == 1
    assert writes[0]["model_providers"]["layer4"]["wire_api"] == "responses"
    assert "wire_api=responses" in message


def test_sync_codex_to_server_profile_mode_uses_effective_local_wire_api(isolated_ssh, monkeypatch):
    security.set_secret("codex:layer4:api_key", "sk-layer4")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com", username="ubuntu"))
    profile_manager.save_codex_profile(
        CodexProfile(
            name="layer4",
            api_key_ref="codex:layer4:api_key",
            model="gpt-5.5",
            model_provider="layer4",
            custom_base_url="https://layer4.example.com/v1",
            custom_wire_api=None,
        )
    )

    fake_client = object()
    writes = []
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "write_remote_codex_config", lambda client, data, profile=None: writes.append(json.loads(json.dumps(data))))
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", lambda client, data, profile=None: None)
    monkeypatch.setattr(persistent_env, "set_remote_user_env", lambda client, data: None)
    monkeypatch.setattr(
        sync_manager,
        "_remote_benchmark_codex_wire_api",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("profile mode must not benchmark")),
    )

    message = sync_manager.sync_codex_to_server("remote", "layer4", wire_api_mode="profile")

    assert len(writes) == 1
    assert writes[0]["model_providers"]["layer4"]["wire_api"] == "responses"
    assert "wire_api=responses" in message


def test_remote_codex_wire_api_benchmark_handles_empty_output(monkeypatch):
    profile = CodexProfile(
        name="layer4",
        model="gpt-5.5",
        model_provider="layer4",
        custom_base_url="https://layer4.example.com/v1",
    )

    monkeypatch.setattr(sync_manager, "_remote_codex_base_url", lambda config, p: "https://layer4.example.com/v1")
    monkeypatch.setattr(sync_manager, "_remote_codex_model", lambda config, p: "gpt-5.5")
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
        name="layer4",
        model="gpt-5.5",
        model_provider="layer4",
        custom_base_url="https://layer4.example.com/v1",
    )

    monkeypatch.setattr(sync_manager, "_remote_codex_base_url", lambda config, p: "https://layer4.example.com/v1")
    monkeypatch.setattr(sync_manager, "_remote_codex_model", lambda config, p: "gpt-5.5")
    monkeypatch.setattr(
        sync_manager.ssh_manager,
        "execute_command_with_status",
        lambda *args, **kwargs: (0, '{"success": false, "error": "invalid payload"}\n', ""),
    )

    result = sync_manager._remote_benchmark_codex_wire_api(object(), profile, {}, "sk-test")

    assert result.success is False
    assert result.error == "invalid payload"


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
    written = {}
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_claude_settings", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda client, profile=None: {})
    monkeypatch.setattr(
        remote_config,
        "write_remote_claude_settings",
        lambda client, data, profile=None: written.setdefault("settings", data),
    )
    monkeypatch.setattr(
        remote_config,
        "write_remote_claude_config",
        lambda client, data, profile=None: written.setdefault("config", data),
    )
    monkeypatch.setattr(
        remote_config,
        "read_remote_vscode_settings",
        lambda client: {
            "claudeCode.initialPermissionMode": "bypassPermissions",
            "claudeCode.allowDangerouslySkipPermissions": True,
        },
    )
    monkeypatch.setattr(remote_config, "write_remote_vscode_settings", lambda client, data: written.setdefault("vscode", data))

    message = sync_manager.sync_claude_to_server("remote", "relay")

    assert written["settings"]["permissions"]["defaultMode"] == "default"
    assert written["settings"]["skipDangerousModePermissionPrompt"] is False
    assert written["vscode"]["claudeCode.initialPermissionMode"] == "default"
    assert written["vscode"]["claudeCode.allowDangerouslySkipPermissions"] is False
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
    written = {}
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_claude_settings", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "write_remote_claude_settings", lambda client, data, profile=None: written.setdefault("settings", data))
    monkeypatch.setattr(remote_config, "write_remote_claude_config", lambda client, data, profile=None: written.setdefault("config", data))
    monkeypatch.setattr(remote_config, "read_remote_vscode_settings", lambda client: (_ for _ in ()).throw(AssertionError("non-root should not read VS Code settings")))
    monkeypatch.setattr(remote_config, "write_remote_vscode_settings", lambda client, data: (_ for _ in ()).throw(AssertionError("non-root should not write VS Code settings")))

    message = sync_manager.sync_claude_to_server("remote", "relay")

    assert written["settings"]["permissions"]["defaultMode"] == "bypassPermissions"
    assert "已兼容 root 登录" not in message


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
    written = {}
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_settings",
        lambda client, profile=None: {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "old-token",
                "ANTHROPIC_API_KEY": "old-token",
                "ANTHROPIC_BASE_URL": "https://relay.example.com",
            },
            "model": "deepseek-chat",
        },
    )
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda client, profile=None: {"primaryApiKey": "old-token"})
    monkeypatch.setattr(
        remote_config,
        "write_remote_claude_credentials",
        lambda client, data, profile=None: written.setdefault("credentials", (client, data, profile)),
    )
    monkeypatch.setattr(
        remote_config,
        "write_remote_claude_settings",
        lambda client, data, profile=None: written.setdefault("settings", (client, data, profile)),
    )
    monkeypatch.setattr(
        remote_config,
        "write_remote_claude_config",
        lambda client, data, profile=None: written.setdefault("config", (client, data, profile)),
    )

    message = sync_manager.sync_claude_account_to_server("remote", "work")

    assert written["credentials"] == (fake_client, credentials, profile_manager.list_ssh_profiles()[0])
    assert "env" not in written["settings"][1]
    assert written["settings"][1]["model"] == "claude-sonnet-4"
    assert "primaryApiKey" not in written["config"][1]
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
    written = {}
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_settings",
        lambda client, profile=None: {"permissions": {"defaultMode": "bypassPermissions"}},
    )
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "write_remote_claude_credentials", lambda client, data, profile=None: written.setdefault("credentials", data))
    monkeypatch.setattr(remote_config, "write_remote_claude_settings", lambda client, data, profile=None: written.setdefault("settings", data))
    monkeypatch.setattr(remote_config, "write_remote_claude_config", lambda client, data, profile=None: written.setdefault("config", data))
    monkeypatch.setattr(remote_config, "read_remote_vscode_settings", lambda client: {"claudeCode.initialPermissionMode": "bypassPermissions"})
    monkeypatch.setattr(remote_config, "write_remote_vscode_settings", lambda client, data: written.setdefault("vscode", data))

    message = sync_manager.sync_claude_account_to_server("remote", "work")

    assert written["settings"]["permissions"]["defaultMode"] == "default"
    assert written["settings"]["skipDangerousModePermissionPrompt"] is False
    assert written["vscode"]["claudeCode.initialPermissionMode"] == "default"
    assert written["vscode"]["claudeCode.allowDangerouslySkipPermissions"] is False
    assert "已兼容 root 登录" in message


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
    written = {}
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda client, profile=None: {"model_provider": "custom"})
    monkeypatch.setattr(
        remote_config,
        "write_remote_codex_auth",
        lambda client, data, profile=None: written.setdefault("auth", (client, data, profile)),
    )
    monkeypatch.setattr(
        remote_config,
        "write_remote_codex_config",
        lambda client, data, profile=None: written.setdefault("config", (client, data, profile)),
    )

    message = sync_manager.sync_codex_account_to_server("remote", "work")

    assert written["auth"][0] is fake_client
    assert written["auth"][1]["auth_mode"] == "chatgpt"
    assert "OPENAI_API_KEY" not in written["auth"][1]
    assert written["auth"][2].name == "remote"
    assert written["config"][1]["model_provider"] == "openai"
    assert written["config"][1]["cli_auth_credentials_store"] == "file"
    assert "ssh.example.com" in message


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


def test_ssh_remote_file_io_uses_binary_sftp_modes():
    manager = SSHManager()
    sftp = _FakeSFTP()
    client = _FakeClient(sftp)

    assert manager.read_remote_file(client, "/remote.json") == '{"ok": true}'
    manager.write_remote_file(client, "/written.json", '{"saved": true}')

    assert "rb" in sftp.open_modes
    assert "wb" in sftp.open_modes
    assert sftp.files["/written.json"] == b'{"saved": true}'
    assert sftp.posix_rename_calls
    assert not sftp.rename_calls
    assert all("\\" not in path for path in sftp.mkdir_calls)


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


def test_remote_config_uses_sftp_home_fallback_when_home_env_is_empty():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp, command_outputs=["", "", ""])

    remote_config.write_remote_claude_settings(client, {"model": "claude-sonnet-4"})

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
    assert ("Codex API: kimi -> OPENAI_API_KEY", "OPENAI_API_KEY", "moonshot-key") in values
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


def test_remote_dependency_install_commands():
    assert (
        remote_auto_continue._install_command_for_packages("apt-get", ["git", "python"])
        == "DEBIAN_FRONTEND=noninteractive apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y git python3"
    )
    assert remote_auto_continue._install_command_for_packages("pacman", ["python"]) == "pacman -Sy --noconfirm python"
    assert remote_auto_continue._install_command_for_packages("apk", ["git"]) == "apk add --no-cache git"
