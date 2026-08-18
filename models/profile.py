from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

from core.providers import CLAUDE_OFFICIAL_DEFAULT_MODEL


def _profile_fields(cls) -> set[str]:
    return set(cls.__dataclass_fields__)


def _known_data(cls, data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    fields = _profile_fields(cls)
    return {key: value for key, value in data.items() if key in fields}


def _clean_str(value, default: str = "") -> str:
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    return text if text else default


def _clean_optional_str(value) -> Optional[str]:
    text = _clean_str(value)
    return text or None


def _clean_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _clean_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _clean_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = _clean_str(item)
        if text:
            cleaned.append(text)
    return cleaned


def _clean_choice(value, choices: set[str], default: str) -> str:
    text = _clean_str(value, default).lower()
    return text if text in choices else default


CLAUDE_AUTH_SCHEMES = {"auth_token", "api_key"}

CODEX_APPROVAL_POLICIES = ("untrusted", "on-request", "never")
CODEX_SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_LEGACY_CODEX_APPROVAL_POLICIES = {
    "manual": "on-request",
    "auto": "on-request",
}
_LEGACY_CODEX_SANDBOX_MODES = {
    "off": "danger-full-access",
}


def normalize_claude_auth_scheme(value, default: str = "auth_token") -> str:
    """Normalize the persisted Claude authentication contract.

    ``auth_token`` maps to ``ANTHROPIC_AUTH_TOKEN`` (Bearer), while
    ``api_key`` maps to ``ANTHROPIC_API_KEY`` (x-api-key).
    """
    fallback = default if default in CLAUDE_AUTH_SCHEMES else "auth_token"
    return _clean_choice(value, CLAUDE_AUTH_SCHEMES, fallback)


def normalize_codex_approval_policy(value, default: str = "untrusted") -> str:
    """Migrate legacy approval values and fail closed for unknown persisted data."""
    fallback = default if default in CODEX_APPROVAL_POLICIES else "untrusted"
    normalized = _clean_str(value).lower()
    normalized = _LEGACY_CODEX_APPROVAL_POLICIES.get(normalized, normalized)
    return normalized if normalized in CODEX_APPROVAL_POLICIES else fallback


def normalize_codex_sandbox_mode(value, default: str = "read-only") -> str:
    """Migrate legacy sandbox values and fail closed for unknown persisted data."""
    fallback = default if default in CODEX_SANDBOX_MODES else "read-only"
    normalized = _clean_str(value).lower()
    normalized = _LEGACY_CODEX_SANDBOX_MODES.get(normalized, normalized)
    return normalized if normalized in CODEX_SANDBOX_MODES else fallback


def validate_codex_approval_policy(value) -> str:
    normalized = _clean_str(value).lower()
    if normalized not in CODEX_APPROVAL_POLICIES:
        allowed = ", ".join(CODEX_APPROVAL_POLICIES)
        raise ValueError(f"Codex 审批策略无效: {value!r}；仅支持 {allowed}")
    return normalized


def validate_codex_sandbox_mode(value) -> str:
    normalized = _clean_str(value).lower()
    if normalized not in CODEX_SANDBOX_MODES:
        allowed = ", ".join(CODEX_SANDBOX_MODES)
        raise ValueError(f"Codex 沙盒模式无效: {value!r}；仅支持 {allowed}")
    return normalized


@dataclass
class ClaudeProfile:
    name: str
    auth_token_ref: str
    base_url: str
    primary_api_key_ref: Optional[str] = None
    auth_scheme: str = "auth_token"
    model: str = CLAUDE_OFFICIAL_DEFAULT_MODEL
    effort_level: str = "high"
    permissions_mode: str = "default"
    skip_dangerous_prompt: bool = False
    permissions_allow: list[str] = field(default_factory=list)
    additional_directories: list[str] = field(default_factory=list)
    provider: str = "anthropic"  # 提供商: anthropic, deepseek, kimi, glm, custom
    custom_provider_name: Optional[str] = None  # 自定义提供商名称

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeProfile":
        item = _known_data(cls, data)
        provider = _clean_str(item.get("provider"), "anthropic")
        default_auth_scheme = "api_key" if provider == "anthropic" else "auth_token"
        return cls(
            name=_clean_str(item.get("name")),
            auth_token_ref=_clean_str(item.get("auth_token_ref")),
            base_url=_clean_str(item.get("base_url")),
            primary_api_key_ref=_clean_optional_str(item.get("primary_api_key_ref")),
            auth_scheme=normalize_claude_auth_scheme(item.get("auth_scheme"), default_auth_scheme),
            model=_clean_str(item.get("model"), CLAUDE_OFFICIAL_DEFAULT_MODEL),
            effort_level=_clean_str(item.get("effort_level"), "high"),
            permissions_mode=_clean_str(item.get("permissions_mode"), "default"),
            skip_dangerous_prompt=_clean_bool(item.get("skip_dangerous_prompt"), False),
            permissions_allow=_clean_str_list(item.get("permissions_allow")),
            additional_directories=_clean_str_list(item.get("additional_directories")),
            provider=provider,
            custom_provider_name=_clean_optional_str(item.get("custom_provider_name")),
        )


@dataclass
class CodexProfile:
    name: str
    api_key_ref: Optional[str] = None
    model: str = "gpt-5.5"
    model_provider: str = "openai"
    model_reasoning_effort: str = "high"
    approval_policy: str = "never"
    sandbox_mode: str = "danger-full-access"
    custom_base_url: Optional[str] = None
    custom_name: Optional[str] = None
    custom_wire_api: Optional[str] = None
    custom_env_key: Optional[str] = None
    custom_requires_openai_auth: bool = False
    disable_response_storage: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    def validated_env_key(self) -> Optional[str]:
        """Validate activation settings and return the effective safe env_key."""
        self.validate_runtime_options()
        if self.custom_requires_openai_auth:
            return None
        from core.providers import ProviderRegistry

        return ProviderRegistry.get_codex_env_key_for_profile(self)

    def validate_runtime_options(self) -> tuple[str, str]:
        """Reject values that current Codex builds do not accept before mutation."""
        return (
            validate_codex_approval_policy(self.approval_policy),
            validate_codex_sandbox_mode(self.sandbox_mode),
        )

    @classmethod
    def from_dict(cls, data: dict) -> "CodexProfile":
        item = _known_data(cls, data)
        return cls(
            name=_clean_str(item.get("name")),
            api_key_ref=_clean_optional_str(item.get("api_key_ref")),
            model=_clean_str(item.get("model"), "gpt-5.5"),
            model_provider=_clean_str(item.get("model_provider"), "openai"),
            model_reasoning_effort=_clean_str(item.get("model_reasoning_effort"), "high"),
            approval_policy=normalize_codex_approval_policy(item.get("approval_policy")),
            sandbox_mode=normalize_codex_sandbox_mode(item.get("sandbox_mode")),
            custom_base_url=_clean_optional_str(item.get("custom_base_url")),
            custom_name=_clean_optional_str(item.get("custom_name")),
            custom_wire_api=_clean_optional_str(item.get("custom_wire_api")),
            custom_env_key=_clean_optional_str(item.get("custom_env_key")),
            custom_requires_openai_auth=_clean_bool(item.get("custom_requires_openai_auth"), False),
            disable_response_storage=_clean_bool(item.get("disable_response_storage"), True),
        )


@dataclass
class ClaudeAccountProfile:
    name: str
    credentials_ref: str
    identity: str = "official-login"
    created_at: str = ""
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeAccountProfile":
        item = _known_data(cls, data)
        return cls(
            name=_clean_str(item.get("name")),
            credentials_ref=_clean_str(item.get("credentials_ref")),
            identity=_clean_str(item.get("identity"), "official-login"),
            created_at=_clean_str(item.get("created_at")),
            notes=_clean_optional_str(item.get("notes")),
        )


@dataclass
class CodexAccountProfile:
    name: str
    auth_json_ref: str
    identity: str = "official-login"
    created_at: str = ""
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CodexAccountProfile":
        item = _known_data(cls, data)
        return cls(
            name=_clean_str(item.get("name")),
            auth_json_ref=_clean_str(item.get("auth_json_ref")),
            identity=_clean_str(item.get("identity"), "official-login"),
            created_at=_clean_str(item.get("created_at")),
            notes=_clean_optional_str(item.get("notes")),
        )


@dataclass
class BackupEntry:
    timestamp: str
    directory: Path
    description: str
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["directory"] = str(d["directory"])
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "BackupEntry":
        item = _known_data(cls, data)
        return cls(
            timestamp=_clean_str(item.get("timestamp")),
            directory=Path(_clean_str(item.get("directory"), ".")),
            description=_clean_str(item.get("description")),
            files=_clean_str_list(item.get("files")),
        )


@dataclass
class SSHProfile:
    name: str
    host: str
    port: int = 22
    username: str = "root"
    auth_type: str = "key"  # "key" or "password"
    password_ref: Optional[str] = None
    private_key_path: Optional[str] = None
    private_key_passphrase_ref: Optional[str] = None
    remote_claude_dir: Optional[str] = None
    remote_codex_dir: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SSHProfile":
        item = _known_data(cls, data)
        return cls(
            name=_clean_str(item.get("name")),
            host=_clean_str(item.get("host")),
            port=_clean_int(item.get("port"), 22, 1, 65535),
            username=_clean_str(item.get("username"), "root"),
            auth_type=_clean_choice(item.get("auth_type"), {"key", "password"}, "key"),
            password_ref=_clean_optional_str(item.get("password_ref")),
            private_key_path=_clean_optional_str(item.get("private_key_path")),
            private_key_passphrase_ref=_clean_optional_str(item.get("private_key_passphrase_ref")),
            remote_claude_dir=_clean_optional_str(item.get("remote_claude_dir")),
            remote_codex_dir=_clean_optional_str(item.get("remote_codex_dir")),
        )


@dataclass
class BrowserProfile:
    name: str
    browser_type: str  # "chrome" or "edge"
    profile_mode: str  # "managed" or "external"
    user_data_dir: str
    start_target: str = "chatgpt"  # "chatgpt", "claude", or "custom"
    custom_url: Optional[str] = None
    notes: Optional[str] = None
    allow_full_reset: bool = False
    created_by_app: bool = False
    browser_executable: Optional[str] = None
    launch_width: int = 1280
    launch_height: int = 900
    launch_language: Optional[str] = "zh-CN"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BrowserProfile":
        item = _known_data(cls, data)
        return cls(
            name=_clean_str(item.get("name")),
            browser_type=_clean_choice(item.get("browser_type"), {"chrome", "edge"}, "chrome"),
            profile_mode=_clean_choice(item.get("profile_mode"), {"managed", "external"}, "managed"),
            user_data_dir=_clean_str(item.get("user_data_dir")),
            start_target=_clean_choice(item.get("start_target"), {"chatgpt", "claude", "custom"}, "chatgpt"),
            custom_url=_clean_optional_str(item.get("custom_url")),
            notes=_clean_optional_str(item.get("notes")),
            allow_full_reset=_clean_bool(item.get("allow_full_reset"), False),
            created_by_app=_clean_bool(item.get("created_by_app"), False),
            browser_executable=_clean_optional_str(item.get("browser_executable")),
            launch_width=_clean_int(item.get("launch_width"), 1280, 640, 7680),
            launch_height=_clean_int(item.get("launch_height"), 900, 480, 4320),
            launch_language=_clean_optional_str(item.get("launch_language")) or "zh-CN",
        )
