import pytest

from core.api_config_parser import parse_api_config_text


CLAUDE_SNIPPET = '''
export ANTHROPIC_BASE_URL="https://sub2api.52ai.pro"
export ANTHROPIC_AUTH_TOKEN="sk-claude-example"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
'''


def test_parse_claude_export_block_and_ignore_unrelated_flags():
    parsed = parse_api_config_text(CLAUDE_SNIPPET)

    assert parsed.profile_type == "claude"
    assert parsed.base_url == "https://sub2api.52ai.pro"
    assert parsed.token == "sk-claude-example"
    assert parsed.auth_scheme == "auth_token"
    assert parsed.provider_id == "custom"
    assert parsed.url_inferred is False


def test_parse_powershell_and_codex_aliases():
    parsed = parse_api_config_text(
        '$env:OPENAI_BASE_URL = "gateway.example.com/v1"; '
        '$env:CODEX_API_KEY="sk-codex"; $env:CODEX_MODEL="gpt-custom"',
        "codex",
    )

    assert parsed.base_url == "https://gateway.example.com/v1"
    assert parsed.token == "sk-codex"
    assert parsed.env_key == "CODEX_API_KEY"
    assert parsed.model == "gpt-custom"
    assert parsed.url_inferred is False


def test_parse_unquoted_cmd_set_block():
    parsed = parse_api_config_text(
        "set ANTHROPIC_BASE_URL=https://proapi.vivijane.pro\n"
        "set ANTHROPIC_AUTH_TOKEN=sk-cmd-unquoted\n"
        "set CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\n"
        "set CLAUDE_CODE_ATTRIBUTION_HEADER=0",
        "claude",
    )

    assert parsed.base_url == "https://proapi.vivijane.pro"
    assert parsed.token == "sk-cmd-unquoted"
    assert parsed.auth_scheme == "auth_token"


def test_parse_json_env_and_infer_url_from_bare_text():
    parsed = parse_api_config_text(
        '{"env":{"ANTHROPIC_AUTH_TOKEN":"sk-json",'
        '"PROVIDER":"deepseek"}}\n'
        "endpoint: api.deepseek.com",
        "claude",
    )

    assert parsed.token == "sk-json"
    assert parsed.provider_id == "deepseek"
    assert parsed.base_url == "https://api.deepseek.com"
    assert parsed.url_inferred is True


def test_parse_markdown_json_fence_and_ignore_schema_url_when_endpoint_is_inferred():
    parsed = parse_api_config_text(
        '''配置如下：
```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "env": {
    "ANTHROPIC_AUTH_TOKEN": "sk-fenced",
    "PROVIDER": "deepseek"
  }
}
```''',
        "claude",
    )

    assert parsed.token == "sk-fenced"
    assert parsed.provider_id == "deepseek"
    assert parsed.base_url == "https://api.deepseek.com/anthropic"
    assert parsed.url_inferred is True


def test_parse_opencode_anthropic_provider_options():
    parsed = parse_api_config_text(
        '''{
          "provider": {
            "anthropic": {
              "options": {
                "baseURL": "https://proapi.vivijane.pro/v1",
                "apiKey": "sk-opencode"
              },
              "npm": "@ai-sdk/anthropic"
            }
          },
          "$schema": "https://opencode.ai/config.json"
        }''',
        "claude",
    )

    # OpenCode commonly includes /v1, while Claude Code appends /v1 itself.
    assert parsed.base_url == "https://proapi.vivijane.pro"
    assert parsed.token == "sk-opencode"
    assert parsed.auth_scheme == "api_key"


@pytest.mark.parametrize(
    ("text", "profile_type", "expected_url", "expected_key"),
    [
        (
            'setx OPENAI_API_KEY "sk-setx"\nsetx OPENAI_BASE_URL "https://relay.example/v1/responses"',
            "codex",
            "https://relay.example/v1",
            "OPENAI_API_KEY",
        ),
        (
            '[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "sk-deep", "User")',
            "codex",
            "https://api.deepseek.com",
            "DEEPSEEK_API_KEY",
        ),
        (
            'set "ANTHROPIC_AUTH_TOKEN=sk-cmd"\nset "ANTHROPIC_BASE_URL=https://relay.example/"',
            "claude",
            "https://relay.example",
            "",
        ),
    ],
)
def test_parse_windows_formats_and_provider_url_fallback(
    text, profile_type, expected_url, expected_key
):
    parsed = parse_api_config_text(text, profile_type)

    assert parsed.base_url == expected_url
    assert parsed.env_key == expected_key
    assert parsed.token.startswith("sk-")


def test_parse_rejects_text_without_secret_or_endpoint():
    with pytest.raises(ValueError, match="API Key/Auth Token"):
        parse_api_config_text("export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1")
