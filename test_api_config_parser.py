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


def test_parse_quoted_shell_values_preserves_ampersands():
    parsed = parse_api_config_text(
        'export OPENAI_BASE_URL="https://relay.example.test/v1?region=cn&mode=fast"\n'
        'export OPENAI_API_KEY="sk-example-with&ampersand"',
        "codex",
    )

    assert parsed.base_url == "https://relay.example.test/v1?region=cn&mode=fast"
    assert parsed.token == "sk-example-with&ampersand"


def test_sniffed_codex_bare_url_preserves_query_when_adding_v1():
    parsed = parse_api_config_text(
        "Codex endpoint: https://relay.example.test?region=cn&path=/\n"
        "API key: sk-example-query-preserved",
        "codex",
    )

    assert parsed.base_url == "https://relay.example.test/v1?region=cn&path=/"
    assert parsed.url_inferred is True


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


def test_parse_opencode_openai_provider_options_infers_codex_and_safe_env_key():
    parsed = parse_api_config_text(
        '''{
          "provider": {
            "openai": {
              "options": {
                "baseURL": "https://relay.example.test/v1/responses",
                "apiKey": "sk-opencode-openai"
              }
            }
          },
          "$schema": "https://opencode.ai/config.json"
        }'''
    )

    assert parsed.profile_type == "codex"
    assert parsed.base_url == "https://relay.example.test/v1"
    assert parsed.token == "sk-opencode-openai"
    assert parsed.env_key == "OPENAI_API_KEY"
    assert parsed.provider_id == "openai"


def test_parse_custom_provider_json_uses_explicit_env_key_and_model():
    parsed = parse_api_config_text(
        '''{
          "providers": {
            "My Relay": {
              "options": {
                "endpoint": "https://relay.example.test/v1/chat/completions",
                "api_key": "sk-custom-provider",
                "envKey": "MY_RELAY_API_KEY",
                "defaultModel": "gpt-relay"
              }
            }
          }
        }''',
        "codex",
    )

    assert parsed.base_url == "https://relay.example.test/v1"
    assert parsed.env_key == "MY_RELAY_API_KEY"
    assert parsed.model == "gpt-relay"
    assert parsed.provider_id == "custom"
    assert parsed.provider_name == "My Relay"


def test_parse_generic_nested_api_key_never_uses_json_path_as_codex_env_key():
    parsed = parse_api_config_text(
        '''{
          "providers": {
            "relay": {
              "options": {
                "base_url": "https://relay.example.test/v1",
                "api_key": "sk-generic-nested"
              }
            }
          }
        }''',
        "codex",
    )

    assert parsed.token == "sk-generic-nested"
    assert parsed.env_key == "OPENAI_API_KEY"
    assert "OPTIONS" not in parsed.env_key


def test_parse_rejects_strong_type_mismatch_but_selects_from_mixed_text():
    claude = (
        "export ANTHROPIC_BASE_URL=https://claude.example.test\n"
        "export ANTHROPIC_AUTH_TOKEN=sk-claude-mixed"
    )
    codex = (
        "export OPENAI_BASE_URL=https://codex.example.test/v1\n"
        "export OPENAI_API_KEY=sk-codex-mixed"
    )

    with pytest.raises(ValueError, match="Claude API 配置"):
        parse_api_config_text(claude, "codex")
    with pytest.raises(ValueError, match="同时检测到 Claude 和 Codex"):
        parse_api_config_text(f"{claude}\n{codex}")

    parsed_claude = parse_api_config_text(f"{claude}\n{codex}", "claude")
    parsed_codex = parse_api_config_text(f"{claude}\n{codex}", "codex")
    assert parsed_claude.token == "sk-claude-mixed"
    assert parsed_claude.base_url == "https://claude.example.test"
    assert parsed_codex.token == "sk-codex-mixed"
    assert parsed_codex.base_url == "https://codex.example.test/v1"


@pytest.mark.parametrize(
    ("text", "profile_type", "expected_url", "expected_env_key"),
    [
        (
            "setx OPENAI_BASE_URL=https://relay.example.test/v1/responses & "
            "setx OPENAI_API_KEY=sk-setx-equals /M",
            "codex",
            "https://relay.example.test/v1",
            "OPENAI_API_KEY",
        ),
        (
            "client --base-url https://relay.example.test/v1/chat/completions "
            "--api-key sk-cli-example --model gpt-relay",
            "codex",
            "https://relay.example.test/v1",
            "OPENAI_API_KEY",
        ),
        (
            'api_key: "sk-local-example"\nendpoint: localhost:8080/v1/models',
            "codex",
            "http://localhost:8080/v1",
            "OPENAI_API_KEY",
        ),
        (
            "$Env:OPENAI_BASE_URL='https://relay.example.test/v1'; "
            "$Env:OPENAI_API_KEY='sk-powershell-case'",
            "codex",
            "https://relay.example.test/v1",
            "OPENAI_API_KEY",
        ),
        (
            "SET OPENAI_BASE_URL=https://relay.example.test/v1\n"
            "SET OPENAI_API_KEY=sk-cmd-case",
            "codex",
            "https://relay.example.test/v1",
            "OPENAI_API_KEY",
        ),
        (
            "client = OpenAI(base_url='https://relay.example.test/v1/responses', "
            "api_key='sk-python-inline', model='gpt-inline')",
            "codex",
            "https://relay.example.test/v1",
            "OPENAI_API_KEY",
        ),
        (
            "set -gx OPENAI_BASE_URL https://relay.example.test/v1\n"
            "set -gx OPENAI_API_KEY sk-fish-shell",
            "codex",
            "https://relay.example.test/v1",
            "OPENAI_API_KEY",
        ),
    ],
)
def test_parse_additional_command_and_local_endpoint_formats(
    text, profile_type, expected_url, expected_env_key
):
    parsed = parse_api_config_text(text, profile_type)

    assert parsed.base_url == expected_url
    assert parsed.env_key == expected_env_key
    assert parsed.token.startswith("sk-")


@pytest.mark.parametrize(
    ("profile_type", "endpoint", "expected"),
    [
        ("claude", "https://relay.example.test/v1/messages", "https://relay.example.test"),
        ("claude", "https://relay.example.test/anthropic/v1/messages", "https://relay.example.test/anthropic"),
        ("claude", "https://relay.example.test/gateway/anthropic/v1/models", "https://relay.example.test/gateway/anthropic"),
        ("codex", "https://relay.example.test/v1/models", "https://relay.example.test/v1"),
        ("codex", "https://relay.example.test/responses", "https://relay.example.test"),
    ],
)
def test_parse_normalizes_resource_urls_to_api_base(profile_type, endpoint, expected):
    key = "ANTHROPIC_AUTH_TOKEN" if profile_type == "claude" else "OPENAI_API_KEY"
    parsed = parse_api_config_text(
        f"{key}=sk-resource-path\nBASE_URL={endpoint}",
        profile_type,
    )

    assert parsed.base_url == expected


def test_parse_quoted_shell_assignments_with_inline_comments():
    parsed = parse_api_config_text(
        'export ANTHROPIC_BASE_URL="https://relay.example.test/gateway/v1/messages" # endpoint\n'
        'export ANTHROPIC_AUTH_TOKEN="sk-inline-comment" # secret',
        "claude",
    )

    assert parsed.base_url == "https://relay.example.test/gateway"
    assert parsed.token == "sk-inline-comment"


@pytest.mark.parametrize(
    "prefix",
    [
        "export ANTHROPIC_AUTH_TOKEN=",
        "$env:ANTHROPIC_AUTH_TOKEN=",
    ],
)
def test_parse_unquoted_hash_in_secret_is_not_mistaken_for_comment(prefix):
    parsed = parse_api_config_text(
        "export ANTHROPIC_BASE_URL=https://relay.example.test # endpoint\n"
        f"{prefix}sk-example-hash#suffix # actual comment",
        "claude",
    )

    assert parsed.base_url == "https://relay.example.test"
    assert parsed.token == "sk-example-hash#suffix"


def test_parse_invalid_explicit_url_falls_back_to_best_valid_sniffed_candidate():
    parsed = parse_api_config_text(
        "API_KEY=sk-valid-fallback\n"
        "BASE_URL=http://remote.example.test\n"
        "API endpoint: https://api.relay.example.test/v1/responses",
        "codex",
    )

    assert parsed.base_url == "https://api.relay.example.test/v1"
    assert parsed.url_inferred is True


def test_parse_ignores_documentation_links_when_provider_fallback_is_available():
    parsed = parse_api_config_text(
        "DEEPSEEK_API_KEY=sk-doc-link\n"
        "See https://github.com/example/project and https://docs.openai.com/example",
        "codex",
    )

    assert parsed.base_url == "https://api.deepseek.com"
    assert parsed.provider_id == "deepseek"


def test_parse_derives_readable_provider_name_for_common_country_suffix():
    parsed = parse_api_config_text(
        "API_KEY=sk-country-domain\nBASE_URL=https://api.relaycorp.co.uk/v1",
        "codex",
    )

    assert parsed.provider_name == "relaycorp"
    assert parsed.name == "relaycorp Codex"


def test_parse_does_not_hide_an_invalid_explicit_port_by_sniffing_its_hostname():
    with pytest.raises(ValueError, match="未找到 API 端点"):
        parse_api_config_text(
            "API_KEY=sk-invalid-port\nBASE_URL=localhost:not-a-port",
            "codex",
        )


def test_parse_vendor_key_uses_provider_fallback_and_claude_auth_contract():
    parsed = parse_api_config_text("DEEPSEEK_API_KEY=sk-deepseek-only", "claude")

    assert parsed.provider_id == "deepseek"
    assert parsed.base_url == "https://api.deepseek.com/anthropic"
    assert parsed.auth_scheme == "auth_token"


@pytest.mark.parametrize(
    ("text", "provider_id", "expected_url"),
    [
        (
            "ZHIPUAI_API_KEY=sk-glm-domestic-only",
            "glm",
            "https://open.bigmodel.cn/api/anthropic",
        ),
        (
            "ZAI_API_KEY=sk-zai-global-only",
            "zai",
            "https://api.z.ai/api/anthropic",
        ),
    ],
)
def test_parse_glm_vendor_key_selects_the_matching_claude_region(
    text, provider_id, expected_url
):
    parsed = parse_api_config_text(text, "claude")

    assert parsed.provider_id == provider_id
    assert parsed.base_url == expected_url
    assert parsed.auth_scheme == "auth_token"


def test_parse_zai_endpoint_is_not_misclassified_as_domestic_glm():
    parsed = parse_api_config_text(
        "ANTHROPIC_AUTH_TOKEN=sk-zai-explicit\n"
        "ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic",
        "claude",
    )

    assert parsed.provider_id == "zai"
    assert parsed.base_url == "https://api.z.ai/api/anthropic"


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
