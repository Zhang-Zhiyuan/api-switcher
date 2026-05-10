import json
from io import BytesIO

import pytest

from core import profile_manager, remote_config, security, sync_manager
from core.ssh_manager import SSHManager, ssh_manager
from core.ssh_profile_builder import build_ssh_profile_from_data
from models.profile import ClaudeAccountProfile, CodexAccountProfile, CodexProfile, SSHProfile


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
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
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

    message = sync_manager.sync_codex_to_server("remote", "relay")

    assert connected["profile"].name == "remote"
    assert written["config"][0] is fake_client
    assert written["auth"][1]["auth_mode"] == "api_key"
    assert written["auth"][1]["OPENAI_API_KEY"] == "sk-relay"
    assert written["auth"][2].name == "remote"
    assert "ssh.example.com" in message


def test_sync_claude_account_to_server_writes_credentials_and_clears_api_overrides(isolated_ssh, monkeypatch):
    credentials = {"claudeAiOauth": {"accessToken": "claude-token"}}
    security.set_secret_json("claude-account:work:credentials", credentials)
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
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
    assert ("/srv/users/alice/.config/codex/auth.json", 0o600) in sftp.chmod_calls


def test_remote_config_uses_sftp_home_fallback_when_home_env_is_empty():
    sftp = _FakeSFTP()
    client = _FakeClient(sftp, command_outputs=["", "", ""])

    remote_config.write_remote_claude_settings(client, {"model": "claude-sonnet-4"})

    assert "/home/fallback/.claude/settings.json" in sftp.files
