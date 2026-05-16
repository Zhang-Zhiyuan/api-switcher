from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class ClaudeProfile:
    name: str
    auth_token_ref: str
    base_url: str
    primary_api_key_ref: Optional[str] = None
    model: str = "claude-sonnet-4"
    effort_level: str = "high"
    permissions_mode: str = "bypassPermissions"
    skip_dangerous_prompt: bool = True
    permissions_allow: list[str] = field(default_factory=list)
    additional_directories: list[str] = field(default_factory=list)
    provider: str = "anthropic"  # 提供商: anthropic, deepseek, kimi, glm, custom
    custom_provider_name: Optional[str] = None  # 自定义提供商名称

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClaudeProfile":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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

    @classmethod
    def from_dict(cls, data: dict) -> "CodexProfile":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
        data = dict(data)
        data["directory"] = Path(data["directory"])
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
