import json
import os
from types import SimpleNamespace

import pytest

from config import paths
from core import auth_parser, backup_manager, codex_env, persistent_env, profile_manager, remote_config, security, switcher, sync_manager, toml_parser
from models.profile import CodexProfile, SSHProfile


@pytest.fixture()
def isolated_codex_flow(tmp_path, monkeypatch):
    secret_store: dict[str, str] = {}
    local_env_writes: list[dict[str, str]] = []
    local_env_deletes: list[list[str]] = []

    monkeypatch.setattr(security, "set_secret", lambda key, value: secret_store.__setitem__(key, value or ""))
    monkeypatch.setattr(security, "get_secret", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "get_secret_strict", lambda key: secret_store.get(key) if key else None)
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(backup_manager, "create_backup", lambda description="": SimpleNamespace(description=description))

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", tmp_path / "codex" / "auth.json")
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", tmp_path / "codex" / "config.toml")
    monkeypatch.setattr(paths, "CODEX_ENV", tmp_path / "codex" / ".env")
    remote_files: dict[str, str] = {}
    monkeypatch.setattr(remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(remote_config, "read_remote_text", lambda _client, path: remote_files.get(path))
    monkeypatch.setattr(
        remote_config,
        "write_remote_text",
        lambda _client, path, content, file_mode=None: remote_files.__setitem__(path, content),
    )
    monkeypatch.setattr(remote_config, "delete_remote_file", lambda _client, path: remote_files.pop(path, None))

    def fake_set_env(updates):
        local_env_writes.append(dict(updates))
        for key, value in updates.items():
            os.environ[key] = value
        return SimpleNamespace(variable_names=list(updates))

    def fake_delete_env(names):
        names = list(names)
        local_env_deletes.append(names)
        for key in names:
            os.environ.pop(key, None)
        return SimpleNamespace(variable_names=names)

    monkeypatch.setattr(persistent_env, "set_local_user_env", fake_set_env)
    monkeypatch.setattr(persistent_env, "delete_local_user_env", fake_delete_env)
    for name in ["OPENAI_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "RELAY_API_KEY"]:
        monkeypatch.delenv(name, raising=False)

    return {
        "tmp_path": tmp_path,
        "secret_store": secret_store,
        "local_env_writes": local_env_writes,
        "local_env_deletes": local_env_deletes,
        "remote_files": remote_files,
    }


def test_codex_dotenv_roundtrip_preserves_unrelated_lines(isolated_codex_flow):
    paths.CODEX_ENV.parent.mkdir(parents=True, exist_ok=True)
    paths.CODEX_ENV.write_text(
        "\n".join([
            "# user note",
            "OTHER_KEY=keep-me",
            "OPENAI_API_KEY=\"old-openai\"",
            "DEEPSEEK_API_KEY=\"old-deepseek\"",
            "",
        ]),
        encoding="utf-8",
    )

    codex_env.update_codex_env(
        updates={"DEEPSEEK_API_KEY": 'sk=deep"seek'},
        deletes=["OPENAI_API_KEY"],
    )

    text = paths.CODEX_ENV.read_text(encoding="utf-8")
    values = codex_env.read_codex_env_values()
    assert "# user note" in text
    assert "OTHER_KEY=keep-me" in text
    assert "OPENAI_API_KEY" not in values
    assert values["DEEPSEEK_API_KEY"] == 'sk=deep"seek'


def test_switch_codex_profiles_use_provider_env_and_preserve_official_auth(isolated_codex_flow):
    security.set_secret("codex:deepseek:api_key", "sk-deepseek")
    security.set_secret("codex:kimi:api_key", "sk-kimi")
    profile_manager.save_codex_profile(
        CodexProfile(name="deepseek", api_key_ref="codex:deepseek:api_key", model="deepseek-v4-flash", model_provider="deepseek")
    )
    profile_manager.save_codex_profile(
        CodexProfile(name="kimi", api_key_ref="codex:kimi:api_key", model="kimi-k2.6", model_provider="kimi")
    )
    auth_parser.write_codex_auth({"auth_mode": "chatgpt", "tokens": {"id_token": "chatgpt-token"}})

    switcher.switch_codex_profile("deepseek")

    assert toml_parser.read_codex_config()["model_providers"]["deepseek"]["env_key"] == "DEEPSEEK_API_KEY"
    assert codex_env.get_codex_env_value("DEEPSEEK_API_KEY") == "sk-deepseek"
    assert "OPENAI_API_KEY" not in auth_parser.read_codex_auth()
    assert auth_parser.read_codex_auth()["tokens"]["id_token"] == "chatgpt-token"
    assert profile_manager.get_current_codex_name() == "deepseek"

    switcher.switch_codex_profile("kimi")

    assert "DEEPSEEK_API_KEY" not in os.environ
    assert codex_env.get_codex_env_value("DEEPSEEK_API_KEY") == ""
    assert os.environ["MOONSHOT_API_KEY"] == "sk-kimi"
    assert codex_env.get_codex_env_value("MOONSHOT_API_KEY") == "sk-kimi"
    assert profile_manager.get_current_codex_name() == "kimi"


def test_import_current_codex_reads_explicit_key_from_codex_dotenv(isolated_codex_flow):
    toml_parser.write_codex_config({
        "model": "relay-model",
        "model_provider": "relay",
        "model_providers": {
            "relay": {
                "name": "Relay",
                "base_url": "https://relay.example.com/v1",
                "env_key": "RELAY_API_KEY",
            }
        },
    })
    auth_parser.write_codex_auth({"auth_mode": "apikey", "OPENAI_API_KEY": "stale-openai"})
    codex_env.set_codex_env({"RELAY_API_KEY": "relay-key"})

    profile = profile_manager.import_current_codex()

    assert profile is not None
    assert profile.custom_env_key == "RELAY_API_KEY"
    assert security.get_secret(profile.api_key_ref) == "relay-key"


def test_import_current_codex_does_not_fallback_from_explicit_env_key_to_stale_openai(
    isolated_codex_flow,
):
    toml_parser.write_codex_config({
        "model": "relay-model",
        "model_provider": "relay",
        "model_providers": {
            "relay": {
                "name": "Relay",
                "base_url": "https://relay.example.com/v1",
                "env_key": "RELAY_API_KEY",
            }
        },
    })
    auth_parser.write_codex_auth({"auth_mode": "apikey", "OPENAI_API_KEY": "stale-openai"})

    assert profile_manager.import_current_codex() is None
    assert profile_manager.list_codex_profiles() == []


def test_explicit_env_key_missing_does_not_match_stale_openai_auth(isolated_codex_flow):
    profile = CodexProfile(
        name="relay",
        api_key_ref="codex:relay:api_key",
        model="relay-model",
        model_provider="relay",
        custom_base_url="https://relay.example.com/v1",
        custom_env_key="RELAY_API_KEY",
    )
    security.set_secret(profile.api_key_ref, "stale-openai")
    config = toml_parser.apply_codex_profile({}, profile)
    auth = {"auth_mode": "apikey", "OPENAI_API_KEY": "stale-openai"}

    assert profile_manager._codex_auth_matches(profile, auth, config) is False


def test_requires_openai_auth_profile_does_not_write_provider_key(isolated_codex_flow):
    profile_manager.save_codex_profile(
        CodexProfile(
            name="proxy-openai-auth",
            model="gpt-5.5",
            model_provider="custom",
            custom_base_url="https://proxy.example.com/v1",
            custom_requires_openai_auth=True,
        )
    )
    os.environ["OPENAI_API_KEY"] = "sk-openai-official"
    auth_parser.write_codex_auth({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-openai-official"})

    switcher.switch_codex_profile("proxy-openai-auth")

    custom = toml_parser.read_codex_config()["model_providers"]["custom"]
    assert custom["requires_openai_auth"] is True
    assert "env_key" not in custom
    assert codex_env.read_codex_env_values() == {}
    assert not paths.CODEX_ENV.exists()
    assert os.environ["OPENAI_API_KEY"] == "sk-openai-official"
    assert auth_parser.read_codex_auth()["OPENAI_API_KEY"] == "sk-openai-official"
    assert profile_manager.get_current_codex_name() == "proxy-openai-auth"


def test_sync_codex_to_server_writes_remote_codex_dotenv(isolated_codex_flow, monkeypatch):
    security.set_secret("codex:deepseek:api_key", "sk-deepseek")
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    profile_manager.save_codex_profile(
        CodexProfile(name="deepseek", api_key_ref="codex:deepseek:api_key", model="deepseek-v4-flash", model_provider="deepseek")
    )
    fake_client = object()
    written: dict[str, object] = {}
    remote_files = isolated_codex_flow["remote_files"]
    state = {
        "config": {},
        "auth": {"auth_mode": "chatgpt", "tokens": {"id": "old"}},
        "dotenv": "OTHER_KEY=keep\nDEEPSEEK_API_KEY=\"old\"\n",
    }
    remote_files["~/.codex/config.toml"] = ""
    remote_files["~/.codex/auth.json"] = json.dumps(state["auth"])
    remote_files["~/.codex/.env"] = state["dotenv"]
    remote_files["/home/test/.api_switcher_env"] = ""

    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)

    def read_config(client, profile=None, **kwargs):
        return state["config"]

    def write_config(client, data, profile=None):
        import tomli_w

        state["config"] = data
        remote_files["~/.codex/config.toml"] = tomli_w.dumps(data)
        written["config"] = data

    def read_auth(client, profile=None, **kwargs):
        return state["auth"]

    def write_auth(client, data, profile=None):
        state["auth"] = data
        remote_files["~/.codex/auth.json"] = json.dumps(data)
        written["auth"] = data

    def write_dotenv(client, content, profile=None):
        state["dotenv"] = content
        remote_files["~/.codex/.env"] = content
        written["codex_env"] = content

    def write_persistent(client, data):
        remote_files["/home/test/.api_switcher_env"] = codex_env.merge_codex_env_text(
            remote_files["/home/test/.api_switcher_env"],
            updates=data,
        )
        written["shell_env"] = data

    monkeypatch.setattr(remote_config, "read_remote_codex_config", read_config)
    monkeypatch.setattr(remote_config, "write_remote_codex_config", write_config)
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", read_auth)
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", write_auth)
    monkeypatch.setattr(remote_config, "read_remote_codex_env", lambda client, profile=None: state["dotenv"])
    monkeypatch.setattr(remote_config, "write_remote_codex_env", write_dotenv)
    monkeypatch.setattr(persistent_env, "set_remote_user_env", write_persistent)

    message = sync_manager.sync_codex_to_server("remote", "deepseek", wire_api_mode="profile")

    assert written["shell_env"] == {"DEEPSEEK_API_KEY": "sk-deepseek"}
    values = codex_env.parse_codex_env_text(str(written["codex_env"]))
    assert values["OTHER_KEY"] == "keep"
    assert values["DEEPSEEK_API_KEY"] == "sk-deepseek"
    assert "OPENAI_API_KEY" not in written["auth"]
    assert "DEEPSEEK_API_KEY" in message


def test_inspect_remote_codex_reads_provider_key_from_remote_dotenv(isolated_codex_flow, monkeypatch):
    profile_manager.save_ssh_profile(SSHProfile(name="remote", host="ssh.example.com"))
    fake_client = object()
    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_claude_settings", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_claude_credentials", lambda client, profile=None: {})
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_config",
        lambda client, profile=None: {
            "model": "deepseek-v4-flash",
            "model_provider": "deepseek",
            "model_providers": {
                "deepseek": {
                    "name": "DeepSeek",
                    "base_url": "https://api.deepseek.com",
                    "env_key": "DEEPSEEK_API_KEY",
                }
            },
        },
    )
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "read_remote_codex_env", lambda client, profile=None: 'DEEPSEEK_API_KEY="sk-remote"\n')

    candidates = sync_manager.inspect_remote_configs("remote")
    codex_candidate = next(candidate for candidate in candidates if candidate.kind == "codex")

    assert codex_candidate.importable is True
    assert codex_candidate.has_api_key is True
    assert codex_candidate.provider == "deepseek"


def test_remote_explicit_env_key_does_not_fallback_to_stale_openai_key(
    isolated_codex_flow,
    monkeypatch,
):
    config = {
        "model_provider": "relay",
        "model_providers": {"relay": {"env_key": "RELAY_API_KEY"}},
    }
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_env",
        lambda *_args, **_kwargs: 'OPENAI_API_KEY="stale-dotenv"\n',
    )
    monkeypatch.setattr(
        sync_manager.ssh_manager,
        "read_remote_file",
        lambda *_args, **_kwargs: "export OPENAI_API_KEY='stale-shell'\n",
    )

    key, env_key = sync_manager._remote_codex_api_key_from_sources(
        object(),
        None,
        config,
        {"OPENAI_API_KEY": "stale-auth"},
    )

    assert key == ""
    assert env_key == "RELAY_API_KEY"
