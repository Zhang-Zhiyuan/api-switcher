import json

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib

from core.auto_continue.codex_provider import (
    CodexProvider,
    _codex_hooks_enabled_from_config,
)


def _read_config(path):
    return tomllib.loads(path.read_text(encoding="utf-8-sig"))


def test_new_install_uses_canonical_hooks_flag_and_preserves_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'model = "gpt-5.5"\n'
        "\n"
        "[projects.demo]\n"
        'trust_level = "trusted"\n',
        encoding="utf-8",
    )

    provider = CodexProvider()
    provider.register_hook()

    config = _read_config(config_path)
    assert config["features"]["hooks"] is True
    assert "codex_hooks" not in config["features"]
    assert "codex_hooks" not in config
    assert config["model"] == "gpt-5.5"
    assert config["projects"]["demo"]["trust_level"] == "trusted"
    assert provider.is_hook_registered()

    provider.unregister_hook()

    config = _read_config(config_path)
    assert config["features"]["hooks"] is False
    assert "codex_hooks" not in config["features"]
    assert config["projects"]["demo"]["trust_level"] == "trusted"
    assert not provider.is_hook_registered()


def test_existing_feature_alias_is_migrated_and_safely_synchronized(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[features] # user feature settings\n"
        "codex_hooks = false # deprecated but retained\n"
        "web_search_request = true\n"
        "\n"
        "[notice]\n"
        'hide_rate_limit_model_nudge = true\n',
        encoding="utf-8",
    )

    provider = CodexProvider()
    provider.register_hook()

    raw = config_path.read_text(encoding="utf-8")
    config = _read_config(config_path)
    assert config["features"]["hooks"] is True
    assert config["features"]["codex_hooks"] is True
    assert config["features"]["web_search_request"] is True
    assert "# deprecated but retained" in raw
    assert "# user feature settings" in raw

    provider.unregister_hook()

    config = _read_config(config_path)
    assert config["features"]["hooks"] is False
    assert config["features"]["codex_hooks"] is False
    assert config["notice"]["hide_rate_limit_model_nudge"] is True


def test_existing_root_alias_is_synchronized_without_adding_feature_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "codex_hooks = false # old Codex builds\n"
        'model = "gpt-5.5"\n'
        "\n"
        "[projects]\n",
        encoding="utf-8",
    )

    provider = CodexProvider()
    provider.register_hook()

    raw = config_path.read_text(encoding="utf-8")
    config = _read_config(config_path)
    assert config["codex_hooks"] is True
    assert config["features"]["hooks"] is True
    assert "codex_hooks" not in config["features"]
    assert "# old Codex builds" in raw

    provider.unregister_hook()

    config = _read_config(config_path)
    assert config["codex_hooks"] is False
    assert config["features"]["hooks"] is False


def test_dotted_legacy_alias_migrates_to_dotted_canonical_key(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "features.codex_hooks = false # legacy dotted key\n"
        'model = "gpt-5.5"\n',
        encoding="utf-8",
    )

    provider = CodexProvider()
    provider._set_codex_hooks_enabled(True)

    config = _read_config(config_path)
    assert config["features"]["hooks"] is True
    assert config["features"]["codex_hooks"] is True
    assert "# legacy dotted key" in config_path.read_text(encoding="utf-8")

    provider._set_codex_hooks_enabled(False)

    config = _read_config(config_path)
    assert config["features"]["hooks"] is False
    assert config["features"]["codex_hooks"] is False


def test_feature_flag_reader_prefers_canonical_and_supports_old_aliases():
    assert _codex_hooks_enabled_from_config({"features": {"hooks": True}})
    assert not _codex_hooks_enabled_from_config({
        "features": {"hooks": False, "codex_hooks": True},
        "codex_hooks": True,
    })
    assert _codex_hooks_enabled_from_config({"features": {"codex_hooks": True}})
    assert _codex_hooks_enabled_from_config({"codex_hooks": True})
    assert not _codex_hooks_enabled_from_config({})


def test_registration_status_honors_canonical_flag_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    provider = CodexProvider()
    provider.register_hook()
    assert provider.is_hook_registered()

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "codex_hooks = true\n"
        "\n"
        "[features]\n"
        "hooks = false\n"
        "codex_hooks = true\n",
        encoding="utf-8",
    )
    assert not provider.is_hook_registered()

    config_path.write_text("[features]\ncodex_hooks = true\n", encoding="utf-8")
    assert provider.is_hook_registered()


def test_inline_features_table_is_not_corrupted(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    original = 'features = { web_search_request = true }\nmodel = "gpt-5.5"\n'
    config_path.write_text(original, encoding="utf-8")

    provider = CodexProvider()
    provider._set_codex_hooks_enabled(True)

    raw = config_path.read_text(encoding="utf-8")
    assert "features = { web_search_request = true, hooks = true }" in raw
    assert _read_config(config_path)["features"] == {
        "web_search_request": True,
        "hooks": True,
    }

    provider._set_codex_hooks_enabled(False)

    config = _read_config(config_path)
    assert config["features"]["web_search_request"] is True
    assert config["features"]["hooks"] is False


def test_feature_section_is_inserted_before_array_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[[example_rules]]\n"
        'name = "keep-me"\n',
        encoding="utf-8",
    )

    CodexProvider()._set_codex_hooks_enabled(True)

    config = _read_config(config_path)
    assert config["features"]["hooks"] is True
    assert config["example_rules"] == [{"name": "keep-me"}]


def test_unrelated_hooks_keys_are_never_modified(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[mcp_server.example]\n"
        "hooks = false # belongs to the user's MCP server\n",
        encoding="utf-8",
    )

    CodexProvider()._set_codex_hooks_enabled(True)

    config = _read_config(config_path)
    assert config["features"]["hooks"] is True
    assert config["mcp_server"]["example"]["hooks"] is False
    assert "# belongs to the user's MCP server" in config_path.read_text(encoding="utf-8")


def test_uninstall_restores_preexisting_enabled_feature(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[features]\n"
        "hooks = true # enabled by the user\n",
        encoding="utf-8",
    )

    provider = CodexProvider()
    provider.register_hook()
    provider.unregister_hook()

    assert _read_config(config_path)["features"]["hooks"] is True
    assert "# enabled by the user" in config_path.read_text(encoding="utf-8")
    assert not provider.get_hooks_feature_state_path().exists()


def test_user_hook_group_metadata_survives_register_and_unregister(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    hooks_path = tmp_path / "hooks.json"
    user_group = {
        "matcher": "Bash(git status:*)",
        "customField": {"owner": "user"},
        "hooks": [{"command": "powershell.exe -File user_stop.ps1", "timeout": 7}],
    }
    hooks_path.write_text(
        json.dumps({"hooks": {"Stop": [user_group]}}),
        encoding="utf-8",
    )

    provider = CodexProvider()
    provider.register_hook()
    installed = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert installed["hooks"]["Stop"][0] == user_group

    provider.unregister_hook()
    uninstalled = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert uninstalled["hooks"]["Stop"] == [user_group]
    # Keeping the feature enabled prevents preserved user hooks from becoming inert.
    assert _read_config(tmp_path / "config.toml")["features"]["hooks"] is True
    assert not provider.get_hooks_feature_state_path().exists()


def test_invalid_toml_is_not_modified_and_hook_registration_is_rolled_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    hooks_path = tmp_path / "hooks.json"
    invalid_config = 'model = "keep"\nfeatures = { hooks = true\n'
    original_hooks = '{"hooks":{"Stop":[{"hooks":[{"command":"user.ps1"}]}]}}'
    config_path.write_text(invalid_config, encoding="utf-8")
    hooks_path.write_text(original_hooks, encoding="utf-8")

    provider = CodexProvider()
    with pytest.raises(RuntimeError, match="Invalid Codex config.toml"):
        provider.register_hook()

    assert config_path.read_text(encoding="utf-8") == invalid_config
    assert hooks_path.read_text(encoding="utf-8") == original_hooks
    assert not provider.get_hooks_feature_state_path().exists()


def test_feature_write_failure_rolls_back_hooks_config_and_ownership(tmp_path, monkeypatch):
    import core.auto_continue.codex_provider as provider_module

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config_path = tmp_path / "config.toml"
    hooks_path = tmp_path / "hooks.json"
    original_config = '[features]\nhooks = false\nmodel_catalog = "user"\n'
    original_hooks = json.dumps({
        "hooks": {
            "Stop": [{
                "matcher": "user-only",
                "hooks": [{"command": "user.ps1"}],
            }],
        },
    })
    config_path.write_text(original_config, encoding="utf-8")
    hooks_path.write_text(original_hooks, encoding="utf-8")

    original_atomic_write = provider_module.atomic_write_text

    def fail_config_write(path, content, *args, **kwargs):
        if path == config_path and "hooks = true" in content:
            raise OSError("simulated config write failure")
        return original_atomic_write(path, content, *args, **kwargs)

    monkeypatch.setattr(provider_module, "atomic_write_text", fail_config_write)
    provider = CodexProvider()
    with pytest.raises(RuntimeError, match="simulated config write failure"):
        provider.register_hook()

    assert config_path.read_text(encoding="utf-8") == original_config
    assert hooks_path.read_text(encoding="utf-8") == original_hooks
    assert not provider.get_hooks_feature_state_path().exists()
