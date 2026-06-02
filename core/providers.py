"""Provider presets for Claude Code, Codex CLI, and OpenAI-compatible APIs."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderConfig:
    name: str
    display_name: str
    default_base_url: str
    default_model: str
    supported_models: list[str]
    reasoning_efforts: list[str]
    requires_auth: bool
    auth_header: str
    wire_api: str = "responses"
    requires_openai_auth: bool = False
    codex_base_url: Optional[str] = None
    codex_env_key: str = "OPENAI_API_KEY"
    claude_base_url: Optional[str] = None
    claude_env: dict[str, str] = field(default_factory=dict)
    claude_supported: bool = True
    codex_supported: bool = True
    notes: str = ""

    def base_url_for_claude(self) -> str:
        return self.claude_base_url if self.claude_base_url is not None else self.default_base_url

    def base_url_for_codex(self) -> str:
        return self.codex_base_url if self.codex_base_url is not None else self.default_base_url


CODEX_REASONING_EFFORTS = ["minimal", "low", "medium", "high", "xhigh"]
CLAUDE_CODE_EFFORTS = ["low", "medium", "high", "xhigh"]
CODEX_WIRE_APIS = {"responses"}
CLAUDE_CODE_MODEL_ALIASES = [
    "default",
    "best",
    "sonnet",
    "sonnet[1m]",
    "opus[1m]",
    "opus",
    "opusplan",
    "haiku",
]


PROVIDERS = {
    "anthropic": ProviderConfig(
        name="anthropic",
        display_name="Anthropic Claude",
        default_base_url="https://api.anthropic.com",
        default_model="claude-sonnet-4",
        supported_models=[
            "sonnet",
            "sonnet[1m]",
            "opus[1m]",
            "opus",
            "opusplan",
            "haiku",
            "claude-opus-4-7",
            "claude-opus-4-7[1m]",
            "claude-sonnet-4-6",
            "claude-sonnet-4-6[1m]",
            "claude-haiku-4-5",
            "claude-opus-4",
            "claude-sonnet-4",
            "claude-haiku-4",
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
        ],
        reasoning_efforts=CLAUDE_CODE_EFFORTS,
        requires_auth=True,
        auth_header="x-api-key",
        codex_supported=False,
    ),
    "deepseek": ProviderConfig(
        name="deepseek",
        display_name="DeepSeek",
        default_base_url="https://api.deepseek.com",
        claude_base_url="https://api.deepseek.com/anthropic",
        default_model="deepseek-v4-flash",
        supported_models=[
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
        reasoning_efforts=CLAUDE_CODE_EFFORTS,
        requires_auth=True,
        auth_header="Authorization",
        wire_api="responses",
        requires_openai_auth=False,
        codex_env_key="DEEPSEEK_API_KEY",
        notes="DeepSeek Codex uses the Responses wire API required by current Codex CLI builds; Claude Code uses the Anthropic-compatible endpoint.",
    ),
    "kimi": ProviderConfig(
        name="kimi",
        display_name="Kimi (Moonshot)",
        default_base_url="https://api.moonshot.ai/v1",
        claude_base_url="",
        default_model="kimi-k2.6",
        supported_models=[
            "kimi-k2.6",
            "kimi-k2.5",
            "kimi-k2",
            "kimi-k2-thinking",
            "kimi-k2-thinking-turbo",
            "kimi-k2-turbo-preview",
            "kimi-k2-0905-preview",
            "kimi-k2-0711-preview",
            "moonshot-v1-8k",
            "moonshot-v1-32k",
            "moonshot-v1-128k",
        ],
        reasoning_efforts=[],
        requires_auth=True,
        auth_header="Authorization",
        wire_api="responses",
        requires_openai_auth=False,
        codex_env_key="MOONSHOT_API_KEY",
        claude_supported=False,
        notes="Kimi uses the Responses wire API required by current Codex CLI builds. Use https://api.moonshot.cn/v1 instead if your key belongs to the China platform.",
    ),
    "glm": ProviderConfig(
        name="glm",
        display_name="GLM (Zhipu/Z.ai)",
        default_base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        claude_base_url="",
        default_model="GLM-5.1",
        supported_models=[
            "GLM-5.1",
            "GLM-5",
            "GLM-5-Turbo",
            "GLM-4.7",
            "GLM-4.7-Flash",
            "GLM-4.6",
            "GLM-4.5",
            "GLM-4.5-Air",
            "GLM-4.5-air",
        ],
        reasoning_efforts=[],
        requires_auth=True,
        auth_header="Authorization",
        wire_api="responses",
        requires_openai_auth=False,
        codex_env_key="ZHIPUAI_API_KEY",
        claude_env={
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM-5.1",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "GLM-5.1",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "GLM-4.5-air",
        },
        notes="GLM Coding Plan is configured with the Responses wire API required by current Codex CLI builds.",
    ),
    "layer4": ProviderConfig(
        name="layer4",
        display_name="Layer4",
        default_base_url="https://layer4.cc/v1",
        default_model="gpt-5.5",
        supported_models=[
            "gpt-5.5",
            "gpt-5.3-codex",
            "gpt-5.4-mini",
            "gpt-5.4",
            "gpt-5.2",
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "codex-auto-review",
        ],
        reasoning_efforts=CODEX_REASONING_EFFORTS,
        requires_auth=True,
        auth_header="Authorization",
        wire_api="responses",
        requires_openai_auth=False,
        codex_env_key="OPENAI_API_KEY",
        claude_supported=False,
        notes=(
            "Layer4 is configured with the Responses wire API because current Codex CLI builds "
            "reject provider tables that contain wire_api=chat."
        ),
    ),
    "custom": ProviderConfig(
        name="custom",
        display_name="Custom",
        default_base_url="",
        default_model="",
        supported_models=[],
        reasoning_efforts=CODEX_REASONING_EFFORTS,
        requires_auth=True,
        auth_header="Authorization",
        wire_api="responses",
        requires_openai_auth=False,
    ),
}


class ProviderRegistry:
    """Registry helpers for provider presets."""

    @staticmethod
    def get_all_providers() -> list[ProviderConfig]:
        return list(PROVIDERS.values())

    @staticmethod
    def get_claude_providers() -> list[ProviderConfig]:
        return [p for p in PROVIDERS.values() if p.claude_supported]

    @staticmethod
    def get_codex_providers() -> list[ProviderConfig]:
        return [p for p in PROVIDERS.values() if p.codex_supported]

    @staticmethod
    def get_provider_names() -> list[str]:
        return list(PROVIDERS.keys())

    @staticmethod
    def get_provider_display_names() -> list[str]:
        return [p.display_name for p in PROVIDERS.values()]

    @staticmethod
    def get_claude_provider_display_names() -> list[str]:
        return [p.display_name for p in ProviderRegistry.get_claude_providers()]

    @staticmethod
    def get_codex_provider_display_names() -> list[str]:
        return [p.display_name for p in ProviderRegistry.get_codex_providers()]

    @staticmethod
    def get_provider(name: str) -> Optional[ProviderConfig]:
        return PROVIDERS.get(name)

    @staticmethod
    def get_provider_by_display_name(display_name: str) -> Optional[ProviderConfig]:
        for provider in PROVIDERS.values():
            if provider.display_name == display_name:
                return provider
        return None

    @staticmethod
    def get_models(provider_name: str) -> list[str]:
        provider = PROVIDERS.get(provider_name)
        return provider.supported_models if provider else []

    @staticmethod
    def get_reasoning_efforts(provider_name: str) -> list[str]:
        provider = PROVIDERS.get(provider_name)
        return provider.reasoning_efforts if provider else []

    @staticmethod
    def model_supports_max_reasoning(model: str | None) -> bool:
        normalized = str(model or "").strip().lower()
        if not normalized:
            return False
        if normalized in {"opus", "opus[1m]", "opusplan"}:
            return True
        tokenized = normalized
        for separator in ["[", "]", "_", ".", "/", "\\", ":", " "]:
            tokenized = tokenized.replace(separator, "-")
        return "opus" in {token for token in tokenized.split("-") if token}

    @staticmethod
    def get_reasoning_efforts_for_model(
        provider_name: str,
        model: str | None,
        custom_name: str | None = None,
    ) -> list[str]:
        normalized_provider_name = str(provider_name or "").strip()
        provider = PROVIDERS.get(normalized_provider_name)
        if not provider and custom_name:
            provider = ProviderRegistry.get_provider_by_display_name(custom_name)
        if not provider and normalized_provider_name and normalized_provider_name != "openai":
            provider = PROVIDERS.get("custom")
        if not provider or not provider.reasoning_efforts:
            return []

        efforts = list(provider.reasoning_efforts)
        if ProviderRegistry.model_supports_max_reasoning(model) and "xhigh" in efforts and "max" not in efforts:
            efforts.append("max")
        return efforts

    @staticmethod
    def get_default_reasoning_effort_for_model(
        provider_name: str,
        model: str | None,
        custom_name: str | None = None,
    ) -> str:
        efforts = ProviderRegistry.get_reasoning_efforts_for_model(provider_name, model, custom_name)
        if ProviderRegistry.model_supports_max_reasoning(model) and "max" in efforts:
            return "max"
        if "xhigh" in efforts:
            return "xhigh"
        if "high" in efforts:
            return "high"
        return efforts[0] if efforts else ""

    @staticmethod
    def supports_reasoning_effort(provider_name: str) -> bool:
        provider = PROVIDERS.get(provider_name)
        return bool(provider and provider.reasoning_efforts)

    @staticmethod
    def get_default_base_url(provider_name: str) -> str:
        provider = PROVIDERS.get(provider_name)
        return provider.default_base_url if provider else ""

    @staticmethod
    def get_claude_base_url(provider_name: str) -> str:
        provider = PROVIDERS.get(provider_name)
        return provider.base_url_for_claude() if provider else ""

    @staticmethod
    def get_codex_base_url(provider_name: str) -> str:
        provider = PROVIDERS.get(provider_name)
        return provider.base_url_for_codex() if provider else ""

    @staticmethod
    def get_codex_env_key(provider_name: str, custom_env_key: str | None = None, custom_name: str | None = None) -> str:
        custom = str(custom_env_key or "").strip()
        if custom:
            return custom
        provider = PROVIDERS.get(provider_name)
        if not provider and custom_name:
            provider = ProviderRegistry.get_provider_by_display_name(custom_name)
        return provider.codex_env_key if provider else "OPENAI_API_KEY"

    @staticmethod
    def get_codex_env_key_for_profile(profile) -> str:
        return ProviderRegistry.get_codex_env_key(
            getattr(profile, "model_provider", "openai"),
            getattr(profile, "custom_env_key", None),
            getattr(profile, "custom_name", None),
        )

    @staticmethod
    def normalize_codex_wire_api(wire_api: str | None) -> str | None:
        value = str(wire_api or "").strip().lower()
        return value if value in CODEX_WIRE_APIS else None

    @staticmethod
    def get_codex_wire_api(
        provider_name: str,
        custom_wire_api: str | None = None,
        custom_name: str | None = None,
    ) -> str:
        custom = ProviderRegistry.normalize_codex_wire_api(custom_wire_api)
        if custom:
            return custom
        provider = PROVIDERS.get(provider_name)
        if not provider and custom_name:
            provider = ProviderRegistry.get_provider_by_display_name(custom_name)
        return ProviderRegistry.normalize_codex_wire_api(provider.wire_api if provider else None) or "responses"

    @staticmethod
    def get_codex_wire_api_for_profile(profile) -> str:
        return ProviderRegistry.get_codex_wire_api(
            getattr(profile, "model_provider", "openai"),
            getattr(profile, "custom_wire_api", None),
            getattr(profile, "custom_name", None),
        )

    @staticmethod
    def get_codex_runtime_env_keys_for_profile(profile) -> list[str]:
        """Environment variable names that should carry the Codex API key.

        Codex provider tables can point to provider-specific keys such as
        DEEPSEEK_API_KEY, while some Codex versions and wrappers still check
        OPENAI_API_KEY directly. Keep the provider key first for config
        fidelity, and always add OPENAI_API_KEY as a compatibility fallback.
        """
        keys = []
        for key in (ProviderRegistry.get_codex_env_key_for_profile(profile), "OPENAI_API_KEY"):
            key = str(key or "").strip()
            if key and key not in keys:
                keys.append(key)
        return keys

    @staticmethod
    def get_default_model(provider_name: str) -> str:
        provider = PROVIDERS.get(provider_name)
        return provider.default_model if provider else ""
