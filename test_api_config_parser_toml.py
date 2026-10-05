"""Copied Codex TOML must retain one provider's complete credential pair."""
import json

import pytest

from core.api_config_parser import parse_api_config_text


def _config(*, selected="first", inline=True, env_key="FIRST_API_KEY", reverse=False):
    providers = [
        ("first", "https://first.example.test/v1", "synthetic-first-key", env_key),
        ("second", "https://second.example.test/v1", "synthetic-second-key", "SECOND_API_KEY"),
    ]
    if reverse:
        providers.reverse()
    lines = ['model = "gpt-synthetic"']
    if selected is not None:
        lines.append(f"model_provider = {json.dumps(selected)}")
    for label, endpoint, token, key in providers:
        lines.extend([f"[model_providers.{label}]", f"base_url = {json.dumps(endpoint)}",
                      f"env_key = {json.dumps(key)}", 'wire_api = "responses"'])
        if inline:
            lines.append(f"api_key = {json.dumps(token)}")
    return "\n".join(lines)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("wrapper", ["{}", "```toml\n{}\n```", "配置：\n```toml\n{}\n```\n复制完成。"])
def test_toml_selected_provider_never_uses_last_section_credentials(reverse, wrapper):
    result = parse_api_config_text(wrapper.format(_config(reverse=reverse)), "codex")
    assert result.base_url == "https://first.example.test/v1"
    assert result.token == "synthetic-first-key"
    assert result.provider_name == "first"
    assert result.env_key == "FIRST_API_KEY"
    assert result.model == "gpt-synthetic"


@pytest.mark.parametrize("wrapper", ["{}", "```toml\n{}\n```"])
@pytest.mark.parametrize("auth_source", ["shell", "auth_json", "json_env"])
def test_toml_provider_resolves_only_its_copied_env_key(wrapper, auth_source):
    variables = {"OPENAI_API_KEY": "synthetic-first-key", "SECOND_API_KEY": "synthetic-second-key"}
    extra = ("\n".join(f"export {key}={value}" for key, value in variables.items())
             if auth_source == "shell" else json.dumps({"env": variables})
             if auth_source == "json_env" else json.dumps(variables))
    text = wrapper.format(_config(inline=False, env_key="OPENAI_API_KEY")) + "\n" + extra
    result = parse_api_config_text(text, "codex")
    assert result.base_url == "https://first.example.test/v1"
    assert result.token == "synthetic-first-key"
    assert result.env_key == "OPENAI_API_KEY"


@pytest.mark.parametrize("selected", [None, "missing"])
def test_ambiguous_or_missing_toml_selection_cannot_fall_back(selected):
    with pytest.raises(ValueError):
        parse_api_config_text(_config(selected=selected), "codex")


@pytest.mark.parametrize("missing", ["endpoint", "token"])
def test_incomplete_selected_toml_provider_cannot_borrow_from_other_provider(missing):
    text = _config()
    remove = 'base_url = "https://first.example.test/v1"' if missing == "endpoint" else 'api_key = "synthetic-first-key"'
    text = text.replace(remove, "")
    with pytest.raises(ValueError) as error:
        parse_api_config_text(text, "codex")
    assert "synthetic-" not in str(error.value)


def test_toml_env_reference_never_reads_real_environment(monkeypatch):
    monkeypatch.setenv("FIRST_API_KEY", "synthetic-process-must-not-be-used")
    with pytest.raises(ValueError):
        parse_api_config_text(_config(inline=False), "codex")


@pytest.mark.parametrize("damage", ["duplicate", "invalid"])
def test_malformed_toml_is_not_silently_flattened(damage):
    text = _config()
    text += '\nbase_url = "https://other.example.test/v1"' if damage == "duplicate" else '\ninvalid = [broken'
    with pytest.raises(ValueError) as error:
        parse_api_config_text(text, "codex")
    assert "synthetic-" not in str(error.value)


def test_dotted_inline_provider_tables_are_scoped_before_flattening():
    text = '''model_provider = "first"
model_providers.first = {base_url = "https://first.example.test/v1", api_key = "synthetic-first-key"}
model_providers.second = {base_url = "https://second.example.test/v1", api_key = "synthetic-second-key"}
'''
    result = parse_api_config_text(text, "codex")
    assert result.base_url == "https://first.example.test/v1"
    assert result.token == "synthetic-first-key"


def test_two_distinct_toml_blocks_are_ambiguous_not_merged():
    first = "```toml\n" + _config() + "\n```"
    second = "```toml\n" + _config(selected="second") + "\n```"
    with pytest.raises(ValueError, match="多个"):
        parse_api_config_text(first + "\n" + second, "codex")


def test_unfenced_toml_with_fenced_auth_json_keeps_named_environment_reference():
    text = _config(inline=False) + '\n```json\n{"FIRST_API_KEY":"synthetic-first-key"}\n```'
    result = parse_api_config_text(text, "codex")
    assert result.token == "synthetic-first-key"
    assert result.base_url == "https://first.example.test/v1"


def test_copied_json_environment_fragments_keep_their_original_precedence():
    text = _config(inline=False) + '\n{"FIRST_API_KEY":"synthetic-original-key"}'
    text += '\n{"FIRST_API_KEY":"synthetic-later-key"}'
    result = parse_api_config_text(text, "codex")
    assert result.token == "synthetic-original-key"


def test_valid_toml_multiline_string_does_not_turn_embedded_markdown_into_a_provider():
    text = _config() + '\nnotes = """Documentation:\n```toml\n[model_providers.not-an-active-provider]\n```\n"""'
    result = parse_api_config_text(text, "codex")
    assert result.token == "synthetic-first-key"
    assert result.base_url == "https://first.example.test/v1"


def test_non_toml_snippet_does_not_eagerly_import_toml_dependency(monkeypatch):
    import builtins
    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name in {"tomllib", "tomli"}:
            pytest.fail("ordinary pasted JSON must not import a TOML dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", isolated_import)
    result = parse_api_config_text('API_KEY=synthetic-key\nBASE_URL=https://relay.example.test/v1', "codex")
    assert result.token == "synthetic-key"


def test_older_python_uses_lazy_tomli_fallback(monkeypatch):
    import builtins
    try:
        import tomllib as compatible_parser
    except ModuleNotFoundError:
        import tomli as compatible_parser
    original_import = builtins.__import__
    imports = []

    def isolated_import(name, *args, **kwargs):
        if name == "tomllib":
            imports.append(name)
            raise ModuleNotFoundError(name)
        if name == "tomli":
            imports.append(name)
            return compatible_parser
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", isolated_import)
    result = parse_api_config_text(_config(), "codex")
    assert result.token == "synthetic-first-key"
    assert imports == ["tomllib", "tomli"]


@pytest.mark.parametrize("single", [False, True])
def test_toml_array_provider_tables_never_fall_back_to_flat_assignment_sniffing(single):
    text = _config().replace("[model_providers.first]", "[[model_providers.first]]")
    text = text.replace("[model_providers.second]", "[[model_providers.second]]")
    if single:
        text = text.split("[[model_providers.second]]")[0]
    with pytest.raises(ValueError) as failure:
        parse_api_config_text(text, "codex")
    assert "synthetic-" not in str(failure.value)


@pytest.mark.parametrize("entry", [
    [{"base_url": "https://first.example.test/v1", "api_key": "synthetic-first-key"}],
    "https://first.example.test/v1",
])
def test_non_object_json_provider_entries_never_use_unrelated_credentials(entry):
    data = {"model_provider": "first", "model_providers": {"first": entry},
            "env": {"OPENAI_API_KEY": "synthetic-unrelated-key"}}
    with pytest.raises(ValueError) as failure:
        parse_api_config_text(json.dumps(data), "codex")
    assert "synthetic-" not in str(failure.value)
