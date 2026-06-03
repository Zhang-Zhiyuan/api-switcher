#!/usr/bin/env python
"""Fix remaining test assertion errors after provider updates."""
import re
from pathlib import Path

test_file = Path("test_provider_config.py")
content = test_file.read_text(encoding="utf-8")

# Fix test_openai URL typo and behavior
content = re.sub(
    r'def test_openai_codex_preset.*?assert provider\.base_url_for_codex\(\) == "https://openai\.cc/v1"',
   'def test_openai_codex_preset_uses_responses_wire_api():\n    provider = ProviderRegistry.get_provider("openai")\n    assert provider is not None\n    assert provider.codex_supported is True\n    assert provider.claude_supported is False\n    assert provider.base_url_for_codex() == "https://api.openai.com/v1"',
    content,
    flags=re.DOTALL
)

# Fix model_providers assertion for openai
content = re.sub(
    r'(def test_openai_codex_preset.*?assert config\["model"\] == "gpt-5\.5").*?(def test_codex_wire_api)',
    r'\1\n    assert config["model_provider"] == "openai"\n    # OpenAI official does not write model_providers table\n    assert "model_providers" not in config or not config.get("model_providers")\n\n\n\2',
    content,
    flags=re.DOTALL
)

# Fix test_codex_wire_api to use deepseek not openai
content = re.sub(
    r'def test_codex_wire_api_defaults_and_invalid_values_use_provider_preset\(\):.*?provider = ProviderRegistry\.get_provider\("openai"\)',
    'def test_codex_wire_api_defaults_and_invalid_values_use_provider_preset():\n    provider = ProviderRegistry.get_provider("deepseek")',
    content,
  flags=re.DOTALL
)
content = re.sub(
    r'(def test_codex_wire_api.*?)assert ProviderRegistry\.get_codex_wire_api\("openai"\)',
    r'\1assert ProviderRegistry.get_codex_wire_api("deepseek")',
    content,
    flags=re.DOTALL,
    count=1
)
content = re.sub(
    r'(def test_codex_wire_api.*?)assert ProviderRegistry\.get_codex_wire_api\("openai", "auto"\)',
    r'\1assert ProviderRegistry.get_codex_wire_api("deepseek", "auto")',
    content,
    flags=re.DOTALL,
    count=1
)
content = re.sub(
    r'(def test_codex_wire_api.*?)assert ProviderRegistry\.get_codex_wire_api\("openai", "invalid"\)',
    r'\1assert ProviderRegistry.get_codex_wire_api("deepseek", "invalid")',
    content,
    flags=re.DOTALL,
    count=1
)
content = re.sub(
    r'(def test_codex_wire_api.*?CodexProfile\(\s+name=)"openai"',
    r'\1"deepseek"',
    content,
    flags=re.DOTALL
)
content = re.sub(
    r'(def test_codex_wire_api.*?model=)"gpt-5\.5"',
    r'\1"deepseek-v4-flash"',
    content,
    flags=re.DOTALL
)
content = re.sub(
    r'(def test_codex_wire_api.*?model_provider=)"openai"',
    r'\1"deepseek"',
    content,
    flags=re.DOTALL
)
content = re.sub(
    r'(def test_codex_wire_api.*?)assert config\["model_providers"\]\["openai"\]',
    r'\1assert config["model_providers"]["deepseek"]',
    content,
    flags=re.DOTALL
)

# Fix test_reasoning line 149: openai doesn't support claude models
content = re.sub(
    r'assert ProviderRegistry\.get_reasoning_efforts_for_model\("openai", "claude-opus-4-7"\) == \[\]',
    'assert ProviderRegistry.get_reasoning_efforts_for_model("anthropic", "gpt-5.5") == []',
    content
)

# Fix test_health_check URL typo
content = re.sub(
    r'(def test_health_check.*?"base_url": )"https://openai\.cc/v1"',
    r'\1"https://api.openai.com/v1"',
    content,
    flags=re.DOTALL
)

test_file.write_text(content, encoding="utf-8")
print(f"Fixed {test_file}")
