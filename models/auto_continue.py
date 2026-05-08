from dataclasses import dataclass, field, asdict
from typing import Optional
import re


@dataclass
class AutoContinueSettings:
    """Settings for auto-continue functionality."""
    enabled: bool = False
    max_continuations: int = 3
    continuation_prompt: str = "Please continue from where you left off. Complete any remaining work."
    apply_to_subagents: bool = False  # Claude Code only
    conservative_mode: bool = True  # stop_hook_active=true 时直接允许停止

    # 错误恢复设置
    error_recovery_enabled: bool = False  # 是否启用错误自动恢复
    max_error_recoveries: int = 3  # 单个会话最大恢复次数

    # Git版本管理设置
    git_auto_snapshot: bool = True  # 是否自动创建git快照（默认开启）
    git_snapshot_on_start: bool = True  # 对话开始时创建快照
    git_snapshot_on_recovery: bool = True  # 错误恢复前创建快照

    # Incomplete patterns (regex)
    incomplete_patterns: list[str] = field(default_factory=lambda: [
        r"(?i)(still|remaining|todo|wip|work in progress|not (yet )?complete)",
        r"(?i)(will|need to|should|must).{0,50}(implement|add|create|fix|test|verify)",
        r"(?i)(next|following) steps?:",
        r"(?i)to be (done|completed|implemented)",
    ])

    # Blocker patterns (regex) - 遇到这些就不续跑
    blocker_patterns: list[str] = field(default_factory=lambda: [
        r"(?i)(error|failed|cannot|unable to|blocked by)",
        r"(?i)(missing|not found|does not exist)",
        r"(?i)(need.{0,30}(your|user) (input|decision|approval|confirmation))",
        r"(?i)which (option|approach|method) (do you|would you like)",
    ])

    def validate(self) -> tuple[bool, str]:
        """Validate settings. Returns (is_valid, error_message)."""
        # Validate max_continuations
        if not isinstance(self.max_continuations, int) or self.max_continuations < 0:
            return False, "max_continuations must be a non-negative integer"

        if self.max_continuations > 100:
            return False, "max_continuations too large (max: 100)"

        # Validate max_error_recoveries
        if not isinstance(self.max_error_recoveries, int) or self.max_error_recoveries < 0:
            return False, "max_error_recoveries must be a non-negative integer"

        if self.max_error_recoveries > 10:
            return False, "max_error_recoveries too large (max: 10)"

        # Validate continuation_prompt
        if not isinstance(self.continuation_prompt, str) or not self.continuation_prompt.strip():
            return False, "continuation_prompt cannot be empty"

        # Validate regex patterns
        for pattern in self.incomplete_patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                return False, f"Invalid incomplete pattern '{pattern}': {e}"

        for pattern in self.blocker_patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                return False, f"Invalid blocker pattern '{pattern}': {e}"

        return True, ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AutoContinueSettings":
        """Create settings from dict with validation."""
        # Filter to known fields only
        known_fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}

        # Create instance
        instance = cls(**known_fields)

        # Validate
        is_valid, error = instance.validate()
        if not is_valid:
            raise ValueError(f"Invalid settings: {error}")

        return instance


@dataclass
class ProviderStatus:
    """Status of a provider's auto-continue installation."""
    provider_name: str  # "codex" or "claude"
    enabled: bool = False
    hook_script_exists: bool = False
    hook_registered: bool = False
    guidance_installed: bool = False
    error_recovery_installed: bool = False  # 错误恢复是否已安装
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)
