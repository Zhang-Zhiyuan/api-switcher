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
    monkeypatch.setattr(security, "delete_secret", lambda key: secret_store.pop(key, None) if key else None)
    monkeypatch.setattr(backup_manager, "create_backup", lambda description="": SimpleNamespace(description=description))

    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    monkeypatch.setattr(auth_parser, "CODEX_AUTH", tmp_path / "codex" / "auth.json")
    monkeypatch.setattr(toml_parser, "CODEX_CONFIG", tmp_path / "codex" / "config.toml")
    monkeypatch.setattr(paths, "CODEX_ENV", tmp_path / "codex" / ".env")

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

    monkeypatch.setattr(sync_manager.ssh_manager, "connect", lambda profile: fake_client)
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda client, profile=None: {})
    monkeypatch.setattr(remote_config, "write_remote_codex_config", lambda client, data, profile=None: written.setdefault("config", data))
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda client, profile=None: {"auth_mode": "chatgpt", "tokens": {"id": "old"}})
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", lambda client, data, profile=None: written.setdefault("auth", data))
    monkeypatch.setattr(remote_config, "read_remote_codex_env", lambda client, profile=None: "OTHER_KEY=keep\nDEEPSEEK_API_KEY=\"old\"\n")
    monkeypatch.setattr(remote_config, "write_remote_codex_env", lambda client, content, profile=None: written.setdefault("codex_env", content))
    monkeypatch.setattr(persistent_env, "set_remote_user_env", lambda client, data: written.setdefault("shell_env", data))

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
