import json

import pytest

from core.api_config_parser import parse_api_config_text


@pytest.mark.parametrize("kind, own_prefix, other_prefix", [
    ("codex", "OPENAI", "ANTHROPIC"),
    ("claude", "ANTHROPIC", "OPENAI"),
])
@pytest.mark.parametrize("missing", ["token", "url"])
@pytest.mark.parametrize("format_name", ["shell", "json"])
def test_mixed_clients_do_not_borrow_missing_credentials_or_url(
    kind, own_prefix, other_prefix, missing, format_name,
):
    values = {
        f"{other_prefix}_BASE_URL": "https://other.example.test/v1",
        f"{other_prefix}_API_KEY": "synthetic-other-secret",
        f"{own_prefix}_MODEL": "synthetic-current-model",
    }
    if missing == "token":
        values[f"{own_prefix}_BASE_URL"] = "https://current.example.test/v1"
    else:
        values[f"{own_prefix}_API_KEY"] = "synthetic-current-secret"
    text = (
        json.dumps({"env": values}) if format_name == "json"
        else "\n".join(f"export {key}={value}" for key, value in values.items())
    )
    with pytest.raises(ValueError) as failure:
        parse_api_config_text(text, kind)
    assert "synthetic-other-secret" not in str(failure.value)
    assert "synthetic-current-secret" not in str(failure.value)


def _multi_provider_config(*, active=None, incomplete=False):
    data = {
        "model_providers": {
            "first": {"base_url": "https://first.example.test/v1"},
            "second": {
                "base_url": "https://second.example.test/v1",
                "api_key": "synthetic-second-secret",
                "env_key": "SECOND_API_KEY",
            },
        },
    }
    if active is not None:
        data["model_provider"] = active
    if not incomplete:
        data["model_providers"]["first"]["api_key"] = "synthetic-first-secret"
    return json.dumps(data)


def test_multiple_unselected_providers_are_not_silently_merged():
    with pytest.raises(ValueError, match="多个"):
        parse_api_config_text(_multi_provider_config(incomplete=True), "codex")


def test_explicit_selected_provider_is_parsed_as_one_credential_pair():
    parsed = parse_api_config_text(_multi_provider_config(active="second"), "codex")
    assert parsed.base_url == "https://second.example.test/v1"
    assert parsed.token == "synthetic-second-secret"
    assert parsed.env_key == "SECOND_API_KEY"
    assert parsed.provider_name == "second"


@pytest.mark.parametrize("missing", ["api_key", "base_url"])
def test_selected_provider_cannot_borrow_from_inactive_provider(missing):
    data = json.loads(_multi_provider_config(active="first"))
    data["model_providers"]["first"].pop(missing)
    with pytest.raises(ValueError):
        parse_api_config_text(json.dumps(data), "codex")


def test_selected_provider_missing_from_config_does_not_pick_another():
    with pytest.raises(ValueError):
        parse_api_config_text(_multi_provider_config(active="missing"), "codex")


@pytest.mark.parametrize("kind, provider, expected_secret", [
    ("claude", "anthropic", "synthetic-claude-secret"),
    ("codex", "openai", "synthetic-codex-secret"),
])
def test_explicit_client_selects_matching_opencode_provider(kind, provider, expected_secret):
    data = {"provider": {
        "anthropic": {"options": {
            "baseURL": "https://claude.example.test/v1", "apiKey": "synthetic-claude-secret",
        }},
        "openai": {"options": {
            "baseURL": "https://codex.example.test/v1", "apiKey": "synthetic-codex-secret",
        }},
    }}
    parsed = parse_api_config_text(json.dumps(data), kind)
    assert parsed.token == expected_secret
    assert parsed.provider_id == provider


def test_complete_mixed_client_inputs_still_select_matching_pair():
    text = (
        "ANTHROPIC_BASE_URL=https://claude.example.test\n"
        "ANTHROPIC_AUTH_TOKEN=synthetic-claude-secret\n"
        "OPENAI_BASE_URL=https://codex.example.test/v1\n"
        "OPENAI_API_KEY=synthetic-codex-secret"
    )
    for kind in ("claude", "codex"):
        parsed = parse_api_config_text(text, kind)
        assert parsed.token == f"synthetic-{kind}-secret"
        assert f"{kind}.example.test" in parsed.base_url


@pytest.mark.parametrize("kind, own_prefix, other_prefix", [
    ("codex", "OPENAI", "ANTHROPIC"),
    ("claude", "ANTHROPIC", "OPENAI"),
])
def test_invalid_mixed_client_url_cannot_fall_back_to_other_client(kind, own_prefix, other_prefix):
    text = (
        f"{own_prefix}_API_KEY=synthetic-current-secret\n"
        f"{own_prefix}_BASE_URL=https://current.example.test:invalid\n"
        f"{other_prefix}_BASE_URL=https://other.example.test/v1\n"
        f"{other_prefix}_API_KEY=synthetic-other-secret"
    )
    with pytest.raises(ValueError):
        parse_api_config_text(text, kind)


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "Configuration:\n{}\nEnd."])
def test_selected_provider_scoping_works_in_copied_json_wrappers(wrapper):
    parsed = parse_api_config_text(wrapper.format(_multi_provider_config(active="second")), "codex")
    assert parsed.token == "synthetic-second-secret"
    assert parsed.base_url == "https://second.example.test/v1"


def test_opencode_model_prefix_selects_provider_without_combining_others():
    data = json.loads(_multi_provider_config())
    data["provider"] = data.pop("model_providers")
    data["model"] = "second/gpt-example"
    parsed = parse_api_config_text(json.dumps(data), "codex")
    assert parsed.token == "synthetic-second-secret"
    assert parsed.base_url == "https://second.example.test/v1"


@pytest.mark.parametrize("container", ["model_providers", "modelProviders"])
def test_codex_slash_model_is_not_interpreted_as_provider_selector(container):
    data = {
        "model": "deepseek/deepseek-chat",
        container: {"custom": {
            "base_url": "https://relay.example.test/v1",
            "api_key": "synthetic-custom-secret",
        }},
    }
    parsed = parse_api_config_text(json.dumps(data), "codex")
    assert parsed.model == "deepseek/deepseek-chat"
    assert parsed.base_url == "https://relay.example.test/v1"
    assert parsed.token == "synthetic-custom-secret"


def test_unknown_and_known_provider_pair_is_ambiguous_without_selection():
    data = {"provider": {
        "openai": {"baseURL": "https://first.example.test", "apiKey": "synthetic-first-secret"},
        "relay": {"baseURL": "https://second.example.test", "apiKey": "synthetic-second-secret"},
    }}
    with pytest.raises(ValueError, match="多个"):
        parse_api_config_text(json.dumps(data), "codex")


def test_ambiguous_provider_error_never_exposes_credentials():
    with pytest.raises(ValueError) as failure:
        parse_api_config_text(_multi_provider_config(), "codex")
    assert "synthetic-" not in str(failure.value)


@pytest.mark.parametrize("source", ["json", "shell"])
def test_selected_provider_env_key_is_the_only_credential_source(source):
    data = {
        "model_provider": "second",
        "model_providers": {
            "first": {"base_url": "https://first.example.test/v1", "env_key": "FIRST_API_KEY"},
            "second": {"base_url": "https://second.example.test/v1", "env_key": "SECOND_API_KEY"},
        },
    }
    variables = {"FIRST_API_KEY": "synthetic-first-secret", "SECOND_API_KEY": "synthetic-second-secret"}
    if source == "json":
        data["env"] = variables
        text = json.dumps(data)
    else:
        text = json.dumps(data) + "\n" + "\n".join(f"export {key}={value}" for key, value in variables.items())
    parsed = parse_api_config_text(text, "codex")
    assert parsed.base_url == "https://second.example.test/v1"
    assert parsed.token == "synthetic-second-secret"
    assert parsed.env_key == "SECOND_API_KEY"


@pytest.mark.parametrize("source", ["json", "shell"])
@pytest.mark.parametrize("inline", [True, False])
def test_selected_provider_pair_takes_priority_over_global_client_settings(source, inline):
    options = {"base_url": "https://relay.example.test/v1", "env_key": "RELAY_API_KEY"}
    if inline:
        options["api_key"] = "synthetic-inline-secret"
    variables = {
        "OPENAI_API_KEY": "synthetic-other-secret",
        "OPENAI_BASE_URL": "https://other.example.test/v1",
        "RELAY_API_KEY": "synthetic-env-secret",
    }
    data = {"model_provider": "relay", "model_providers": {"relay": options}}
    if source == "json":
        data["env"] = variables
        text = json.dumps(data)
    else:
        text = json.dumps(data) + "\n" + "\n".join(f"export {key}={value}" for key, value in variables.items())
    parsed = parse_api_config_text(text, "codex")
    assert parsed.base_url == "https://relay.example.test/v1"
    assert parsed.token == ("synthetic-inline-secret" if inline else "synthetic-env-secret")
    assert parsed.env_key == "RELAY_API_KEY"


@pytest.mark.parametrize("source", ["json", "shell"])
def test_missing_selected_provider_env_key_is_not_replaced_by_another_key(source):
    data = {"model_provider": "relay", "model_providers": {"relay": {
        "base_url": "https://relay.example.test/v1", "env_key": "MISSING_API_KEY",
    }}}
    if source == "json":
        data["env"] = {"OPENAI_API_KEY": "synthetic-other-secret"}
        text = json.dumps(data)
    else:
        text = json.dumps(data) + "\nexport OPENAI_API_KEY=synthetic-other-secret"
    with pytest.raises(ValueError) as failure:
        parse_api_config_text(text, "codex")
    assert "synthetic-other-secret" not in str(failure.value)


def test_camel_case_provider_selector_keeps_selected_pair_authority():
    data = json.loads(_multi_provider_config(active="second"))
    data["modelProvider"] = data.pop("model_provider")
    parsed = parse_api_config_text(json.dumps(data), "codex")
    assert parsed.base_url == "https://second.example.test/v1"
    assert parsed.token == "synthetic-second-secret"


@pytest.mark.parametrize("source", ["json", "shell"])
def test_explicit_env_key_does_not_resolve_through_inferred_aliases(source):
    data = {"model_provider": "relay", "model_providers": {"relay": {
        "base_url": "https://relay.example.test/v1", "env_key": "API_KEY",
    }}}
    if source == "json":
        data["env"] = {"OTHER_API_KEY": "synthetic-other-secret"}
        text = json.dumps(data)
    else:
        text = json.dumps(data) + "\nexport OTHER_API_KEY=synthetic-other-secret"
    with pytest.raises(ValueError, match="没有值"):
        parse_api_config_text(text, "codex")
