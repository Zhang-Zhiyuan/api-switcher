import json
from types import SimpleNamespace

import pytest

from core import auth_parser, parser, persistent_env, profile_manager, remote_config, sync_manager, toml_parser


def _failure(stage_name):
    state = {"fired": False}

    def hit(stage):
        if stage == stage_name and not state["fired"]:
            state["fired"] = True
            raise OSError(f"injected: {stage}")

    return hit


def _install_remote(monkeypatch, failure_stage):
    hit = _failure(failure_stage)
    client = object()
    ssh_profile = SimpleNamespace(
        name="remote",
        host="ssh.example.com",
        username="ubuntu",
        remote_claude_dir="/remote/claude",
        remote_codex_dir="/remote/codex",
    )
    state = {
        "/remote/claude/settings.json": '\ufeff{\r\n  "old": true\r\n}\r\n',
        "/remote/claude/config.json": '{ "keep" : 1 }\n',
        "/remote/claude/.credentials.json": '{\n  "claudeAiOauth": {"accessToken": "old"}\n}\n',
        "/remote/codex/config.toml": '# keep formatting\nmodel_provider = "old"\n',
        "/remote/codex/auth.json": '{ "auth_mode" : "chatgpt", "tokens": {"id": "old"} }\n',
        "/remote/codex/.env": 'OLD_KEY="old"\nKEEP=yes\n',
        "/home/test/.api_switcher_env": "export OLD_KEY='old'\n",
        "/home/test/.profile": "# original profile\n",
    }
    monkeypatch.setattr(sync_manager, "_connect_ssh", lambda _name: (ssh_profile, client))
    monkeypatch.setattr(remote_config, "_remote_home", lambda _client: "/home/test")
    monkeypatch.setattr(remote_config, "read_remote_text", lambda _client, path: state.get(path))
    monkeypatch.setattr(
        remote_config,
        "write_remote_text",
        lambda _client, path, content, file_mode=None: state.__setitem__(path, content),
    )
    monkeypatch.setattr(remote_config, "delete_remote_file", lambda _client, path: state.pop(path, None))

    def read_json(path, strict=False):
        text = state.get(path)
        if text is None:
            return None
        try:
            value = json.loads(text.lstrip("\ufeff"))
            if not isinstance(value, dict):
                raise ValueError("not object")
            return value
        except Exception as error:
            if strict:
                raise RuntimeError("strict JSON failure") from error
            return None

    monkeypatch.setattr(
        remote_config,
        "read_remote_json",
        lambda _client, path, *, strict=False: read_json(path, strict),
    )
    monkeypatch.setattr(
        remote_config,
        "write_remote_json",
        lambda _client, path, data, file_mode=None: state.__setitem__(
            path,
            json.dumps(data, ensure_ascii=False),
        ),
    )

    def read_settings(_client, _profile=None, *, strict=False):
        return read_json("/remote/claude/settings.json", strict)

    def read_claude_config(_client, _profile=None, *, strict=False):
        return read_json("/remote/claude/config.json", strict)

    def read_credentials(_client, _profile=None, *, strict=False):
        return read_json("/remote/claude/.credentials.json", strict)

    def write_json(path, stage):
        def write(_client, data, _profile=None):
            state[path] = json.dumps(data, ensure_ascii=False)
            hit(stage)
        return write

    monkeypatch.setattr(remote_config, "read_remote_claude_settings", read_settings)
    monkeypatch.setattr(remote_config, "read_remote_claude_config", read_claude_config)
    monkeypatch.setattr(remote_config, "read_remote_claude_credentials", read_credentials)
    monkeypatch.setattr(remote_config, "write_remote_claude_settings", write_json("/remote/claude/settings.json", "settings"))
    monkeypatch.setattr(remote_config, "write_remote_claude_config", write_json("/remote/claude/config.json", "claude_config"))
    monkeypatch.setattr(
        remote_config,
        "write_remote_claude_credentials",
        write_json("/remote/claude/.credentials.json", "credentials"),
    )

    def read_codex_config(_client, _profile=None, *, strict=False):
        text = state.get("/remote/codex/config.toml")
        if text is None:
            return None
        try:
            import tomllib
            return tomllib.loads(text)
        except Exception as error:
            if strict:
                raise RuntimeError("strict TOML failure") from error
            return None

    def write_codex_config(_client, data, _profile=None):
        import tomli_w
        state["/remote/codex/config.toml"] = tomli_w.dumps(data)
        hit("codex_config")

    def read_codex_auth(_client, _profile=None, *, strict=False):
        return read_json("/remote/codex/auth.json", strict)

    monkeypatch.setattr(remote_config, "read_remote_codex_config", read_codex_config)
    monkeypatch.setattr(remote_config, "write_remote_codex_config", write_codex_config)
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", read_codex_auth)
    monkeypatch.setattr(remote_config, "write_remote_codex_auth", write_json("/remote/codex/auth.json", "auth"))
    monkeypatch.setattr(remote_config, "read_remote_codex_env", lambda _client, _profile=None: state.get("/remote/codex/.env"))

    def write_codex_env(_client, content, _profile=None):
        state["/remote/codex/.env"] = content
        hit("dotenv")

    monkeypatch.setattr(remote_config, "write_remote_codex_env", write_codex_env)

    def set_remote_env(_client, updates):
        lines = "".join(f"export {key}='{value}'\n" for key, value in updates.items())
        state["/home/test/.api_switcher_env"] = lines
        state["/home/test/.profile"] = "# changed profile\n"
        hit("persistent")

    monkeypatch.setattr(persistent_env, "set_remote_user_env", set_remote_env)

    def delete_remote_env(_client, names):
        state["/home/test/.api_switcher_env"] = persistent_env._remove_env_exports(
            state.get("/home/test/.api_switcher_env", ""),
            names,
        )
        hit("persistent_delete")

    monkeypatch.setattr(persistent_env, "delete_remote_user_env", delete_remote_env)
    monkeypatch.setattr(sync_manager, "_remote_codex_login_status", lambda *_args, **_kwargs: (None, ""))
    return state, ssh_profile, client


@pytest.mark.parametrize("failure_stage", ["settings", "claude_config"])
def test_remote_claude_api_failure_restores_original_text(monkeypatch, failure_stage):
    state, _ssh, _client = _install_remote(monkeypatch, failure_stage)
    before = dict(state)
    target = SimpleNamespace(
        name="api",
        auth_token_ref="token",
        primary_api_key_ref=None,
        base_url="https://relay.example.test",
        provider="custom",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_claude_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_claude_profile", lambda _target: True)
    monkeypatch.setattr(sync_manager.security, "get_secret", lambda _ref: "secret")
    monkeypatch.setattr(parser, "apply_claude_profile", lambda _data, _target: {"new": True})
    monkeypatch.setattr(parser, "apply_claude_config", lambda _data, _target: {"new": True})

    with pytest.raises(OSError, match="injected"):
        sync_manager.sync_claude_to_server("remote", "api")
    assert state == before


def test_remote_claude_post_write_mismatch_triggers_rollback(monkeypatch):
    state, _ssh, _client = _install_remote(monkeypatch, "never")
    before = dict(state)
    target = SimpleNamespace(
        name="api",
        auth_token_ref="token",
        primary_api_key_ref=None,
        base_url="https://relay.example.test",
        provider="custom",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_claude_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_claude_profile", lambda _target: True)
    monkeypatch.setattr(sync_manager.security, "get_secret", lambda _ref: "secret")
    monkeypatch.setattr(parser, "apply_claude_profile", lambda _data, _target: {"new": True})
    monkeypatch.setattr(parser, "apply_claude_config", lambda _data, _target: {"new": True})
    reads = {"count": 0}

    def inconsistent_config(_client, _profile=None, *, strict=False):
        reads["count"] += 1
        return {"keep": 1} if reads["count"] == 1 else {"wrong": True}

    monkeypatch.setattr(remote_config, "read_remote_claude_config", inconsistent_config)
    with pytest.raises(RuntimeError, match="回读不一致"):
        sync_manager.sync_claude_to_server("remote", "api")
    assert state == before


@pytest.mark.parametrize("failure_stage", ["credentials", "persistent_delete", "settings", "claude_config"])
def test_remote_claude_account_failure_restores_original_text(monkeypatch, failure_stage):
    state, _ssh, _client = _install_remote(monkeypatch, failure_stage)
    state["/home/test/.api_switcher_env"] = (
        "export ANTHROPIC_AUTH_TOKEN='old'\nexport KEEP='yes'\n"
    )
    before = dict(state)
    target = SimpleNamespace(name="official")
    credentials = {"claudeAiOauth": {"accessToken": "new"}}
    monkeypatch.setattr(profile_manager, "list_claude_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "load_claude_account_credentials", lambda _target: credentials)
    monkeypatch.setattr(profile_manager, "_validate_claude_account_credentials", lambda _data: (True, ""))
    monkeypatch.setattr(parser, "clear_claude_api_overrides", lambda _data: {"official": True})
    monkeypatch.setattr(parser, "clear_claude_config_auth", lambda _data: {"keep": 1})

    with pytest.raises(OSError, match="injected"):
        sync_manager.sync_claude_account_to_server("remote", "official")
    assert state == before


def test_remote_claude_account_clears_only_managed_persistent_env(monkeypatch):
    state, _ssh, _client = _install_remote(monkeypatch, "never")
    state["/home/test/.api_switcher_env"] = (
        "export ANTHROPIC_AUTH_TOKEN='old'\n"
        "export ANTHROPIC_BASE_URL='https://relay.example.test'\n"
        "export KEEP='yes'\n"
    )
    target = SimpleNamespace(name="official")
    credentials = {"claudeAiOauth": {"accessToken": "new"}}
    monkeypatch.setattr(profile_manager, "list_claude_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "load_claude_account_credentials", lambda _target: credentials)
    monkeypatch.setattr(profile_manager, "_validate_claude_account_credentials", lambda _data: (True, ""))

    sync_manager.sync_claude_account_to_server("remote", "official")

    assert state["/home/test/.api_switcher_env"] == "export KEEP='yes'\n"


def test_remote_claude_account_rollback_restores_shell_startup_files(monkeypatch):
    state, _ssh, _client = _install_remote(monkeypatch, "never")
    state["/home/test/.api_switcher_env"] = "export ANTHROPIC_AUTH_TOKEN='old'\n"
    before = dict(state)
    target = SimpleNamespace(name="official")
    credentials = {"claudeAiOauth": {"accessToken": "new"}}
    monkeypatch.setattr(profile_manager, "list_claude_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "load_claude_account_credentials", lambda _target: credentials)
    monkeypatch.setattr(profile_manager, "_validate_claude_account_credentials", lambda _data: (True, ""))

    def delete_remote_env(_client, names):
        state["/home/test/.api_switcher_env"] = persistent_env._remove_env_exports(
            state["/home/test/.api_switcher_env"], names
        )
        state["/home/test/.profile"] = "# accidentally changed\n"
        raise OSError("injected shell mutation")

    monkeypatch.setattr(persistent_env, "delete_remote_user_env", delete_remote_env)

    with pytest.raises(OSError, match="injected shell mutation"):
        sync_manager.sync_claude_account_to_server("remote", "official")

    assert state == before


@pytest.mark.parametrize("failure_stage", ["codex_config", "auth", "persistent", "dotenv"])
def test_remote_codex_api_failure_restores_config_auth_and_both_env_files(monkeypatch, failure_stage):
    state, _ssh, _client = _install_remote(monkeypatch, failure_stage)
    before = dict(state)
    target = SimpleNamespace(
        name="api",
        api_key_ref="key",
        custom_requires_openai_auth=False,
        custom_base_url="https://relay.example.test/v1",
        model_provider="custom",
        validate_runtime_options=lambda: ("never", "danger-full-access"),
        validated_env_key=lambda: "NEW_KEY",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_codex_profile", lambda _target: True)
    monkeypatch.setattr(sync_manager, "_codex_profile_api_key", lambda _target: "new-secret")
    monkeypatch.setattr(toml_parser, "apply_codex_profile", lambda _data, _target: {"model_provider": "new"})
    monkeypatch.setattr(auth_parser, "apply_codex_apikey", lambda _data, _target: {"auth_mode": "chatgpt"})

    with pytest.raises(OSError, match="injected"):
        sync_manager.sync_codex_to_server("remote", "api", wire_api_mode="profile")
    assert state == before


def test_remote_codex_rollback_restores_missing_dotenv_as_missing(monkeypatch):
    state, _ssh, _client = _install_remote(monkeypatch, "dotenv")
    state.pop("/remote/codex/.env")
    before = dict(state)
    target = SimpleNamespace(
        name="api", api_key_ref="key", custom_requires_openai_auth=False,
        custom_base_url="https://relay.example.test/v1", model_provider="custom",
        validate_runtime_options=lambda: ("never", "danger-full-access"),
        validated_env_key=lambda: "NEW_KEY",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_codex_profile", lambda _target: True)
    monkeypatch.setattr(sync_manager, "_codex_profile_api_key", lambda _target: "new-secret")
    monkeypatch.setattr(toml_parser, "apply_codex_profile", lambda _data, _target: {"model_provider": "new"})
    monkeypatch.setattr(auth_parser, "apply_codex_apikey", lambda _data, _target: {})

    with pytest.raises(OSError, match="injected"):
        sync_manager.sync_codex_to_server("remote", "api", wire_api_mode="profile")
    assert state == before


@pytest.mark.parametrize("failure_stage", ["auth", "codex_config", "persistent_delete", "dotenv", "login"])
def test_remote_codex_account_reuses_transaction_and_restores_every_file(monkeypatch, failure_stage):
    state, _ssh, _client = _install_remote(monkeypatch, failure_stage)
    state["/remote/codex/config.toml"] = (
        'model_provider = "old"\n\n[model_providers.old]\nenv_key = "OLD_KEY"\n'
    )
    before = dict(state)
    target = SimpleNamespace(name="official")
    auth = {"auth_mode": "chatgpt", "tokens": {"id_token": "new"}}
    monkeypatch.setattr(profile_manager, "list_codex_account_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "load_codex_account_auth", lambda _target: auth)
    monkeypatch.setattr(profile_manager, "_validate_codex_account_auth", lambda _data: (True, ""))
    monkeypatch.setattr(
        toml_parser,
        "apply_codex_official_account",
        lambda _data: {"model_provider": "openai", "cli_auth_credentials_store": "file"},
    )
    hit = _failure(failure_stage)

    def login(*_args, **_kwargs):
        hit("login")
        return True, "Logged in"

    monkeypatch.setattr(sync_manager, "_remote_codex_login_status", login)

    with pytest.raises(OSError, match="injected"):
        sync_manager.sync_codex_account_to_server("remote", "official")
    assert state == before


def _prepare_remote_claude_clear_state(state):
    state["/remote/claude/settings.json"] = (
        '\ufeff{\r\n  "env": {"ANTHROPIC_AUTH_TOKEN": "old", "KEEP": "yes"},\r\n'
        '  "model": "relay-model"\r\n}\r\n'
    )
    state["/remote/claude/config.json"] = '{ "primaryApiKey" : "old", "keep" : true }\n'
    state["/home/test/.api_switcher_env"] = (
        "export ANTHROPIC_AUTH_TOKEN='old'\nexport KEEP_ENV='yes'\n"
    )


@pytest.mark.parametrize("failure_stage", ["settings", "claude_config", "persistent_delete"])
def test_remote_claude_clear_failure_restores_exact_files(monkeypatch, failure_stage):
    state, ssh_profile, client = _install_remote(monkeypatch, failure_stage)
    _prepare_remote_claude_clear_state(state)
    before = dict(state)

    with pytest.raises(OSError, match="injected"):
        sync_manager._clear_remote_claude_api_info(client, ssh_profile)

    assert state == before


def _prepare_remote_codex_clear_state(state):
    state["/remote/codex/config.toml"] = (
        '# Preserve this exact comment.\nmodel = "relay-model"\nmodel_provider = "old"\n\n'
        '[model_providers.old]\nname = "Old"\nbase_url = "https://relay.example.test/v1"\n'
        'env_key = "OLD_KEY"\nwire_api = "responses"\n'
    )
    state["/remote/codex/auth.json"] = (
        '{\r\n  "auth_mode" : "apikey",\r\n  "OPENAI_API_KEY" : "old",\r\n'
        '  "tokens": {"id_token": "keep"}\r\n}\r\n'
    )
    state["/remote/codex/.env"] = "OLD_KEY=old\nKEEP=yes\n"
    state["/home/test/.api_switcher_env"] = "export OLD_KEY='old'\nexport KEEP='yes'\n"


@pytest.mark.parametrize(
    "failure_stage",
    ["codex_config", "auth", "persistent_delete", "dotenv"],
)
def test_remote_codex_clear_failure_restores_exact_files(monkeypatch, failure_stage):
    state, ssh_profile, client = _install_remote(monkeypatch, failure_stage)
    _prepare_remote_codex_clear_state(state)
    before = dict(state)

    with pytest.raises(OSError, match="injected"):
        sync_manager._clear_remote_codex_api_info(client, ssh_profile)

    assert state == before


def test_remote_codex_clear_dotenv_readback_mismatch_rolls_back(monkeypatch):
    state, ssh_profile, client = _install_remote(monkeypatch, "never")
    _prepare_remote_codex_clear_state(state)
    before = dict(state)
    monkeypatch.setattr(
        remote_config,
        "write_remote_codex_env",
        lambda _client, _content, _profile=None: state.__setitem__(
            "/remote/codex/.env",
            "WRONG=value\n",
        ),
    )

    with pytest.raises(RuntimeError, match=r"Codex \.env.*回读不一致"):
        sync_manager._clear_remote_codex_api_info(client, ssh_profile)

    assert state == before


def test_remote_codex_clear_rollback_keeps_missing_dotenv_missing(monkeypatch):
    state, ssh_profile, client = _install_remote(monkeypatch, "persistent_delete")
    _prepare_remote_codex_clear_state(state)
    state.pop("/remote/codex/.env")
    before = dict(state)

    with pytest.raises(OSError, match="injected"):
        sync_manager._clear_remote_codex_api_info(client, ssh_profile)

    assert state == before
    assert "/remote/codex/.env" not in state


@pytest.mark.parametrize("kind", ["claude", "codex"])
def test_corrupt_remote_config_aborts_before_first_write(monkeypatch, kind):
    state, _ssh, _client = _install_remote(monkeypatch, "never")
    before = dict(state)
    if kind == "claude":
        state["/remote/claude/settings.json"] = "{broken"
        before = dict(state)
        target = SimpleNamespace(
            name="api",
            auth_token_ref="token",
            primary_api_key_ref=None,
            base_url="https://relay.example.test",
            provider="custom",
        )
        monkeypatch.setattr(profile_manager, "list_switchable_claude_profiles", lambda: [target])
        monkeypatch.setattr(profile_manager, "is_third_party_claude_profile", lambda _target: True)
        monkeypatch.setattr(sync_manager.security, "get_secret", lambda _ref: "secret")
        def call():
            return sync_manager.sync_claude_to_server("remote", "api")
    else:
        state["/remote/codex/config.toml"] = "broken = ["
        before = dict(state)
        target = SimpleNamespace(
            name="api", api_key_ref="key", custom_requires_openai_auth=False,
            custom_base_url="https://relay.example.test/v1", model_provider="custom",
            validate_runtime_options=lambda: ("never", "danger-full-access"),
            validated_env_key=lambda: "NEW_KEY",
        )
        monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])
        monkeypatch.setattr(profile_manager, "is_third_party_codex_profile", lambda _target: True)
        monkeypatch.setattr(sync_manager, "_codex_profile_api_key", lambda _target: "secret")
        def call():
            return sync_manager.sync_codex_to_server("remote", "api", wire_api_mode="profile")

    with pytest.raises(RuntimeError, match="strict"):
        call()
    assert state == before


def test_codex_env_key_is_validated_before_ssh_connect(monkeypatch):
    target = SimpleNamespace(
        name="unsafe",
        api_key_ref="key",
        model_provider="custom",
        custom_base_url="https://relay.example.test/v1",
        custom_requires_openai_auth=False,
        validate_runtime_options=lambda: ("never", "danger-full-access"),
        validated_env_key=lambda: (_ for _ in ()).throw(ValueError("unsafe env_key")),
    )
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_codex_profile", lambda _target: True)
    monkeypatch.setattr(sync_manager, "_connect_ssh", lambda _name: (_ for _ in ()).throw(AssertionError("connected")))
    with pytest.raises(ValueError, match="unsafe env_key"):
        sync_manager.sync_codex_to_server("remote", "unsafe")


def test_unsafe_old_codex_env_key_is_not_added_to_cleanup_targets():
    config = {
        "model_provider": "custom",
        "model_providers": {"custom": {"env_key": "SAFE; rm -rf /"}},
    }
    names = sync_manager._remote_codex_account_switch_env_names(config)
    assert "SAFE; rm -rf /" not in names
    assert "OPENAI_API_KEY" in names
    assert "SAFE; rm -rf /" not in sync_manager._remote_codex_api_env_names(config)


def test_remote_codex_active_api_cleanup_targets_only_current_provider():
    config = {
        "model_provider": "deepseek",
        "model_providers": {
            "deepseek": {"name": "DeepSeek", "env_key": "DEEPSEEK_API_KEY"},
            "other": {"env_key": "OTHER_KEY"},
        },
    }

    names = sync_manager._remote_codex_active_api_env_names(config)

    assert set(names) == {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"}
    assert "OTHER_KEY" not in names


def test_sync_all_rejects_conflicting_stored_api_and_account_markers(monkeypatch):
    monkeypatch.setattr(
        profile_manager,
        "list_switchable_claude_profiles",
        lambda: [SimpleNamespace(name="claude-api")],
    )
    monkeypatch.setattr(
        profile_manager,
        "list_claude_account_profiles",
        lambda: [SimpleNamespace(name="claude-account")],
    )
    monkeypatch.setattr(profile_manager, "get_current_claude_name", lambda: None)
    monkeypatch.setattr(profile_manager, "get_current_claude_account_name", lambda: None)
    monkeypatch.setattr(profile_manager, "get_active_claude_name", lambda: "claude-api")
    monkeypatch.setattr(profile_manager, "get_active_claude_account_name", lambda: "claude-account")
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [])
    monkeypatch.setattr(profile_manager, "list_codex_account_profiles", lambda: [])

    with pytest.raises(RuntimeError, match="Claude.*同时指向"):
        sync_manager.sync_all_to_server("remote")


@pytest.mark.parametrize(
    ("product", "provider_id", "expected"),
    (
        ("claude", "anthropic", "只能同步第三方 Claude"),
        ("claude", "openai", "不支持 Claude Code"),
        ("codex", "openai", "只能同步第三方 Codex"),
        ("codex", "anthropic", "不支持 Codex"),
    ),
)
def test_remote_api_sync_rejects_invalid_target_before_ssh(monkeypatch, product, provider_id, expected):
    connect_calls = []
    monkeypatch.setattr(sync_manager, "_connect_ssh", lambda name: connect_calls.append(name))

    if product == "claude":
        target = SimpleNamespace(
            name="invalid",
            provider=provider_id,
            base_url="https://relay.example.test",
            auth_token_ref="token",
            primary_api_key_ref=None,
        )
        monkeypatch.setattr(profile_manager, "list_switchable_claude_profiles", lambda: [target])

        def call():
            return sync_manager.sync_claude_to_server("remote", "invalid")
    else:
        target = SimpleNamespace(
            name="invalid",
            model_provider=provider_id,
            custom_base_url="https://relay.example.test/v1",
            api_key_ref="key",
            custom_requires_openai_auth=False,
            validate_runtime_options=lambda: ("never", "danger-full-access"),
            validated_env_key=lambda: "OPENAI_API_KEY",
        )
        monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])

        def call():
            return sync_manager.sync_codex_to_server("remote", "invalid")

    with pytest.raises(ValueError, match=expected):
        call()

    assert connect_calls == []


def test_remote_codex_sync_validates_runtime_options_before_ssh(monkeypatch):
    target = SimpleNamespace(
        name="invalid-runtime",
        model_provider="custom",
        custom_base_url="https://relay.example.test/v1",
        api_key_ref="key",
        custom_requires_openai_auth=False,
        validate_runtime_options=lambda: (_ for _ in ()).throw(ValueError("invalid runtime options")),
        validated_env_key=lambda: (_ for _ in ()).throw(AssertionError("env validation must run later")),
    )
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])
    monkeypatch.setattr(
        sync_manager,
        "_connect_ssh",
        lambda _name: (_ for _ in ()).throw(AssertionError("SSH must not connect")),
    )

    with pytest.raises(ValueError, match="invalid runtime options"):
        sync_manager.sync_codex_to_server("remote", "invalid-runtime")


def test_inspect_remote_codex_account_ignores_stale_auth_in_keyring_mode(monkeypatch):
    state, ssh_profile, client = _install_remote(monkeypatch, "never")
    state["/remote/codex/config.toml"] = 'cli_auth_credentials_store = "keyring"\n'
    stale_reads = []
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *_args, **_kwargs: stale_reads.append(True) or {
            "auth_mode": "chatgpt",
            "tokens": {"id_token": "stale"},
        },
    )
    monkeypatch.setattr(
        sync_manager,
        "_remote_codex_login_status",
        lambda *_args, **_kwargs: (True, "Logged in using ChatGPT"),
    )

    candidate = sync_manager._inspect_remote_codex_account(client, ssh_profile)

    assert candidate.importable is False
    assert candidate.has_api_key is False
    assert stale_reads == []
    assert "keyring" in candidate.reason
    assert "auth.json" in candidate.reason
    assert "仅作提示" in candidate.reason


def test_pull_remote_codex_account_ignores_stale_auth_in_keyring_mode(monkeypatch):
    state, _ssh_profile, _client = _install_remote(monkeypatch, "never")
    state["/remote/codex/config.toml"] = 'cli_auth_credentials_store = "keyring"\n'
    stale_reads = []
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *_args, **_kwargs: stale_reads.append(True) or {
            "auth_mode": "chatgpt",
            "tokens": {"id_token": "stale"},
        },
    )
    monkeypatch.setattr(
        sync_manager,
        "_remote_codex_login_status",
        lambda *_args, **_kwargs: (True, "Logged in using ChatGPT"),
    )
    monkeypatch.setattr(
        profile_manager,
        "save_codex_account_profile_with_auth",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stale auth must not be saved")),
    )

    with pytest.raises(ValueError, match="keyring.*auth.json"):
        sync_manager.pull_codex_account_from_server("remote")

    assert stale_reads == []


def test_inspect_remote_codex_account_accepts_verified_auto_file_fallback(monkeypatch):
    state, ssh_profile, client = _install_remote(monkeypatch, "never")
    state["/remote/codex/config.toml"] = 'cli_auth_credentials_store = "auto"\n'
    auth_reads = []
    status_calls = []
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *_args, **_kwargs: auth_reads.append(True) or {
            "auth_mode": "chatgpt",
            "tokens": {"id_token": "portable-file-token"},
        },
    )

    def login_status(*_args, **kwargs):
        status_calls.append(kwargs)
        return True, "Logged in using ChatGPT"

    monkeypatch.setattr(sync_manager, "_remote_codex_login_status", login_status)

    candidate = sync_manager._inspect_remote_codex_account(client, ssh_profile)

    assert candidate.importable is True
    assert candidate.has_api_key is True
    assert auth_reads == [True]
    assert status_calls[-1]["credentials_store"] == "file"
    assert "auto" in candidate.reason
    assert "auth.json" in candidate.reason


def test_pull_remote_codex_account_accepts_verified_auto_file_fallback(monkeypatch):
    state, _ssh_profile, _client = _install_remote(monkeypatch, "never")
    state["/remote/codex/config.toml"] = 'cli_auth_credentials_store = "auto"\n'
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {"id_token": "portable-file-token", "account_id": "acct-test"},
    }
    saved = []
    monkeypatch.setattr(remote_config, "read_remote_codex_auth", lambda *_args, **_kwargs: auth)
    monkeypatch.setattr(
        sync_manager,
        "_remote_codex_login_status",
        lambda *_args, **kwargs: (True, "Logged in using ChatGPT")
        if kwargs.get("credentials_store") == "file"
        else (None, ""),
    )
    monkeypatch.setattr(profile_manager, "list_codex_account_profiles", lambda: [])
    monkeypatch.setattr(profile_manager, "refresh_codex_account_snapshot_if_current", lambda _name: False)
    monkeypatch.setattr(
        profile_manager,
        "save_codex_account_profile_with_auth",
        lambda profile, data: saved.append((profile, data)),
    )

    message = sync_manager.pull_codex_account_from_server("remote")

    assert len(saved) == 1
    assert saved[0][1]["tokens"]["account_id"] == "acct-test"
    assert "auto" in message


def test_auto_file_fallback_rejects_cli_validation_failure(monkeypatch):
    state, ssh_profile, client = _install_remote(monkeypatch, "never")
    state["/remote/codex/config.toml"] = 'cli_auth_credentials_store = "auto"\n'
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *_args, **_kwargs: {
            "auth_mode": "chatgpt",
            "tokens": {"id_token": "stale-file-token"},
        },
    )
    monkeypatch.setattr(
        sync_manager,
        "_remote_codex_login_status",
        lambda *_args, **_kwargs: (False, "Not logged in"),
    )

    candidate = sync_manager._inspect_remote_codex_account(client, ssh_profile)

    assert candidate.importable is False
    assert "校验失败" in candidate.reason


def test_remote_claude_cleanup_covers_model_and_effort_overrides():
    assert set(parser.CLAUDE_MODEL_OVERRIDE_ENV_KEYS).issubset(sync_manager.REMOTE_CLAUDE_API_ENV_NAMES)


def test_pull_claude_imports_env_overrides_and_auth_scheme_without_legacy_primary_ref(monkeypatch):
    ssh_profile = SimpleNamespace(host="ssh.example.com")
    monkeypatch.setattr(sync_manager, "_connect_ssh", lambda _name: (ssh_profile, object()))
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_settings",
        lambda *_args, **_kwargs: {
            "env": {
                "ANTHROPIC_API_KEY": "remote-secret",
                "ANTHROPIC_BASE_URL": "https://relay.example.com",
                "ANTHROPIC_MODEL": "remote-model",
                "CLAUDE_CODE_EFFORT_LEVEL": "xhigh",
            },
            "effortLevel": "low",
        },
    )
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(profile_manager, "detect_claude_provider", lambda _settings: "custom")
    saved = []
    monkeypatch.setattr(
        profile_manager,
        "save_claude_profile_with_secrets",
        lambda profile, updates: saved.append((profile, updates)),
    )

    sync_manager.pull_claude_from_server("remote")

    [(profile, updates)] = saved
    assert profile.auth_scheme == "api_key"
    assert profile.model == "remote-model"
    assert profile.effort_level == "xhigh"
    assert profile.primary_api_key_ref is None
    assert updates == {"claude:Remote-remote:auth_token": "remote-secret"}


def _install_local_secret_store(monkeypatch, initial: dict[str, str]):
    secrets = dict(initial)
    monkeypatch.setattr(
        sync_manager.security,
        "get_secret_strict",
        lambda ref: secrets.get(ref) if ref else None,
    )
    monkeypatch.setattr(
        sync_manager.security,
        "get_secret",
        lambda ref: secrets.get(ref) if ref else None,
    )
    monkeypatch.setattr(
        sync_manager.security,
        "set_secret",
        lambda ref, value: secrets.__setitem__(ref, value),
    )
    monkeypatch.setattr(
        sync_manager.security,
        "delete_secret",
        lambda ref: secrets.pop(ref, None),
    )
    return secrets


def _isolate_local_profile_store(monkeypatch, tmp_path):
    monkeypatch.setattr(profile_manager, "PROFILES_FILE", tmp_path / "profiles.json")
    profile_manager.clear_profile_store_cache()


def test_pull_claude_account_rolls_back_overwritten_secret_when_profile_save_fails(
    monkeypatch,
    tmp_path,
):
    name = "Remote Account"
    ref = f"claude-account:{name}:credentials"
    secrets = _install_local_secret_store(monkeypatch, {ref: "old-credential-secret"})
    _isolate_local_profile_store(monkeypatch, tmp_path)
    ssh_profile = SimpleNamespace(host="ssh.example.com")
    credentials = {"claudeAiOauth": {"accessToken": "new-token"}}
    monkeypatch.setattr(sync_manager, "_connect_ssh", lambda _name: (ssh_profile, object()))
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_credentials",
        lambda *_args, **_kwargs: credentials,
    )
    monkeypatch.setattr(
        profile_manager,
        "_validate_claude_account_credentials",
        lambda _value: (True, "ok"),
    )
    monkeypatch.setattr(profile_manager, "_claude_account_identity_from_credentials", lambda _value: "identity")
    monkeypatch.setattr(profile_manager, "_claude_account_preferred_name", lambda _value: name)
    monkeypatch.setattr(profile_manager, "_pick_claude_account_import_name", lambda *_args: name)
    monkeypatch.setattr(
        profile_manager,
        "save_claude_account_profile",
        lambda _profile: (_ for _ in ()).throw(OSError("profile write failed")),
    )

    with pytest.raises(OSError, match="profile write failed"):
        sync_manager.pull_claude_account_from_server("remote")

    assert secrets == {ref: "old-credential-secret"}


def test_pull_claude_api_removes_new_secret_when_profile_save_fails(monkeypatch, tmp_path):
    ref = "claude:Remote-remote:auth_token"
    secrets = _install_local_secret_store(monkeypatch, {})
    _isolate_local_profile_store(monkeypatch, tmp_path)
    ssh_profile = SimpleNamespace(host="ssh.example.com")
    monkeypatch.setattr(sync_manager, "_connect_ssh", lambda _name: (ssh_profile, object()))
    monkeypatch.setattr(
        remote_config,
        "read_remote_claude_settings",
        lambda *_args, **_kwargs: {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "new-token",
                "ANTHROPIC_BASE_URL": "https://relay.example.test",
            },
            "model": "relay-model",
        },
    )
    monkeypatch.setattr(remote_config, "read_remote_claude_config", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(profile_manager, "detect_claude_provider", lambda _settings: "custom")
    monkeypatch.setattr(
        profile_manager,
        "save_claude_profile",
        lambda _profile, previous_name=None, **_kwargs: (
            _ for _ in ()
        ).throw(OSError("profile write failed")),
    )

    with pytest.raises(OSError, match="profile write failed"):
        sync_manager.pull_claude_from_server("remote")

    assert ref not in secrets


def test_pull_codex_api_rolls_back_overwritten_secret_when_profile_save_fails(
    monkeypatch,
    tmp_path,
):
    ref = "codex:Remote-remote:api_key"
    secrets = _install_local_secret_store(monkeypatch, {ref: "old-api-secret"})
    _isolate_local_profile_store(monkeypatch, tmp_path)
    ssh_profile = SimpleNamespace(host="ssh.example.com")
    config = {
        "model": "relay-model",
        "model_provider": "custom",
        "model_providers": {
            "custom": {
                "base_url": "https://relay.example.test/v1",
                "env_key": "RELAY_API_KEY",
                "wire_api": "responses",
            }
        },
    }
    monkeypatch.setattr(sync_manager, "_connect_ssh", lambda _name: (ssh_profile, object()))
    monkeypatch.setattr(remote_config, "read_remote_codex_config", lambda *_args, **_kwargs: config)
    monkeypatch.setattr(
        remote_config,
        "read_remote_codex_auth",
        lambda *_args, **_kwargs: {"RELAY_API_KEY": "new-api-secret"},
    )
    monkeypatch.setattr(
        profile_manager,
        "save_codex_profile",
        lambda _profile, previous_name=None, **_kwargs: (
            _ for _ in ()
        ).throw(OSError("profile write failed")),
    )

    with pytest.raises(OSError, match="profile write failed"):
        sync_manager.pull_codex_from_server("remote")

    assert secrets == {ref: "old-api-secret"}


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "http://relay.example.test/v1",
        "https://user:password@relay.example.test/v1",
        "https://relay.example.test/v1#fragment",
        "https://relay.example.test/v1\nInjected: yes",
    ),
)
def test_sync_claude_rejects_unsafe_base_url_before_ssh(monkeypatch, unsafe_url):
    target = SimpleNamespace(
        name="unsafe",
        auth_token_ref="token",
        primary_api_key_ref=None,
        base_url=unsafe_url,
        provider="custom",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_claude_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_claude_profile", lambda _target: True)
    monkeypatch.setattr(
        sync_manager,
        "_connect_ssh",
        lambda _name: (_ for _ in ()).throw(AssertionError("SSH must not connect")),
    )

    with pytest.raises(ValueError):
        sync_manager.sync_claude_to_server("remote", "unsafe")


@pytest.mark.parametrize(
    "unsafe_url",
    (
        "http://relay.example.test/v1",
        "https://user:password@relay.example.test/v1",
        "https://relay.example.test/v1#fragment",
        "https://relay.example.test/v1\rInjected: yes",
    ),
)
def test_sync_codex_rejects_unsafe_base_url_before_ssh(monkeypatch, unsafe_url):
    target = SimpleNamespace(
        name="unsafe",
        api_key_ref="key",
        model_provider="custom",
        custom_base_url=unsafe_url,
        custom_requires_openai_auth=False,
        validate_runtime_options=lambda: ("never", "danger-full-access"),
        validated_env_key=lambda: (_ for _ in ()).throw(AssertionError("validation order")),
    )
    monkeypatch.setattr(profile_manager, "list_switchable_codex_profiles", lambda: [target])
    monkeypatch.setattr(profile_manager, "is_third_party_codex_profile", lambda _target: True)
    monkeypatch.setattr(
        sync_manager,
        "_connect_ssh",
        lambda _name: (_ for _ in ()).throw(AssertionError("SSH must not connect")),
    )

    with pytest.raises(ValueError):
        sync_manager.sync_codex_to_server("remote", "unsafe")


def test_remote_sync_canonicalizes_implicit_https_before_first_write(monkeypatch):
    state, _ssh, _client = _install_remote(monkeypatch, "never")
    claude_seen = []
    claude = SimpleNamespace(
        name="claude-api",
        auth_token_ref="token",
        primary_api_key_ref=None,
        base_url="Relay.Example.Test/anthropic/",
        provider="custom",
    )
    monkeypatch.setattr(profile_manager, "list_switchable_claude_profiles", lambda: [claude])
    monkeypatch.setattr(profile_manager, "is_third_party_claude_profile", lambda _target: True)
    monkeypatch.setattr(sync_manager.security, "get_secret", lambda _ref: "secret")

    def apply_claude(_settings, profile):
        claude_seen.append(profile.base_url)
        return {"base_url": profile.base_url}

    monkeypatch.setattr(parser, "apply_claude_profile", apply_claude)
    monkeypatch.setattr(parser, "apply_claude_config", lambda _config, _profile: {"ok": True})

    sync_manager.sync_claude_to_server("remote", "claude-api")

    assert claude_seen == ["https://relay.example.test/anthropic"]
    assert json.loads(state["/remote/claude/settings.json"])["base_url"] == claude_seen[0]
