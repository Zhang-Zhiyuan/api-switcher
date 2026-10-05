"""An unrelated vendor variable must not supply an endpoint's credential."""
import json

import pytest

from core.api_config_parser import parse_api_config_text


def _snippet(values, format_name):
    return (json.dumps({"env": values}) if format_name == "json"
            else "\n".join(f"export {key}={value}" for key, value in values.items()))


@pytest.mark.parametrize("format_name", ["shell", "json"])
@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("reverse", [False, True])
def test_vendor_token_matches_explicit_endpoint_not_shortest_variable(format_name, kind, reverse):
    values = {"BASE_URL": "https://api.deepseek.com", "DEEPSEEK_API_KEY": "synthetic-deepseek-key",
              "ZAI_API_KEY": "synthetic-unrelated-key"}
    if reverse:
        values = dict(reversed(list(values.items())))
    result = parse_api_config_text(_snippet(values, format_name), kind)
    assert result.provider_id == "deepseek"
    assert result.token == "synthetic-deepseek-key"
    if kind == "codex":
        assert result.env_key == "DEEPSEEK_API_KEY"


@pytest.mark.parametrize("endpoint", ["", "https://relay.example.test/v1"])
def test_multiple_vendor_keys_without_explicit_vendor_authority_are_ambiguous(endpoint):
    values = {"DEEPSEEK_API_KEY": "synthetic-deepseek-key", "ZAI_API_KEY": "synthetic-unrelated-key"}
    if endpoint:
        values["BASE_URL"] = endpoint
    with pytest.raises(ValueError) as failure:
        parse_api_config_text(_snippet(values, "shell"), "codex")
    assert "synthetic-" not in str(failure.value)


@pytest.mark.parametrize("format_name", ["shell", "json"])
def test_explicit_provider_name_scopes_vendor_key_and_fallback_endpoint(format_name):
    values = {"PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "synthetic-deepseek-key",
              "ZAI_API_KEY": "synthetic-unrelated-key"}
    result = parse_api_config_text(_snippet(values, format_name), "codex")
    assert result.provider_id == "deepseek"
    assert result.token == "synthetic-deepseek-key"
    assert result.base_url == "https://api.deepseek.com"


@pytest.mark.parametrize("key", ["API_KEY", "OPENAI_API_KEY"])
@pytest.mark.parametrize("format_name", ["shell", "json"])
def test_explicit_generic_or_client_key_and_custom_endpoint_ignore_unrelated_vendor(key, format_name):
    values = {"BASE_URL": "https://relay.example.test/v1", key: "synthetic-relay-key",
              "DEEPSEEK_API_KEY": "synthetic-unrelated-key"}
    result = parse_api_config_text(_snippet(values, format_name), "codex")
    assert result.token == "synthetic-relay-key"
    assert result.provider_id == "custom"
    assert result.env_key == "OPENAI_API_KEY"


def test_unrelated_provider_credential_is_not_sent_to_explicit_official_endpoint():
    with pytest.raises(ValueError):
        parse_api_config_text("BASE_URL=https://api.deepseek.com\nZAI_API_KEY=synthetic-zai-key", "codex")
