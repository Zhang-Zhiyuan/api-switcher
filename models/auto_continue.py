from dataclasses import dataclass, field, asdict
from typing import Optional
import re


DEFAULT_INCOMPLETE_PATTERNS = [
    r"(?i)(still|remaining|todo|wip|work in progress|not (yet )?complete)",
    r"(?i)(will|need to|should|must).{0,50}(implement|add|create|fix|test|verify)",
    r"(?i)(next|following) steps?:",
    r"(?i)to be (done|completed|implemented)",
    r"(待办|待完成|待实现|待处理|未完成|未实现|未处理|未修复|未测试|未验证|尚未完成|尚未实现|还没完成|还未完成|进行中)",
    r"(?<![不无])(?:还|仍|仍然|尚|接下来)?\s*(?:需要|需|必须|应当|应该|(?<![不无需])要).{0,50}(实现|添加|新增|创建|修复|测试|验证|检查|处理|完成|继续|优化)",
    r"(下一步|接下来|后续步骤|后续计划|下一阶段)[:：]",
    r"(后续|之后|下一步|接下来).{0,20}(需要|继续|会|将).{0,50}(实现|添加|新增|创建|修复|测试|验证|检查|处理|完成|优化)",
]


DEFAULT_BLOCKER_PATTERNS = [
    r"(?i)(error|failed|cannot|unable to|blocked by)",
    r"(?i)(missing|not found|does not exist)",
    r"(?i)(need.{0,30}(your|user) (input|decision|approval|confirmation))",
    r"(?i)which (option|approach|method) (do you|would you like)",
    r"(错误|失败|无法|不能|被阻塞|卡住|没有权限|权限不足)",
    r"(缺少|找不到|不存在).{0,20}(文件|配置|路径|命令|依赖|参数|信息|凭证|权限|API|api|key|token|模型|账号|目录|环境变量)",
    r"(需要|请|等待|等你|等用户|由你|由用户).{0,40}(输入|确认|选择|决定|授权|批准|同意|提供|补充|回复|告知|指定)",
    r"(请选择|请确认|请提供|请授权|请决定|需要你确认|需要你选择|需要用户确认|等待用户|等你确认)",
]


DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS = ["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"]


def _merge_unique_patterns(patterns: list[str] | None, defaults: list[str]) -> list[str]:
    """Return user patterns plus any missing built-in patterns."""
    merged: list[str] = []
    seen: set[str] = set()
    source = patterns if isinstance(patterns, list) else []

    for pattern in source + defaults:
        value = str(pattern).strip()
        if value and value not in seen:
            merged.append(value)
            seen.add(value)

    return merged


def _merge_unique_strings(values: list[str] | None, defaults: list[str]) -> list[str]:
    """Return user strings plus defaults, deduplicated case-insensitively."""
    merged: list[str] = []
    seen: set[str] = set()
    source = values if isinstance(values, list) else []

    for item in source + defaults:
        value = str(item).strip()
        key = value.casefold()
        if value and key not in seen:
            merged.append(value)
            seen.add(key)

    return merged


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

    auto_approve_permission_requests: bool = False
    auto_approve_max_per_session: int = 0  # 0 means unlimited
    auto_approve_bash: bool = True
    auto_approve_tools: list[str] = field(default_factory=lambda: list(DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS))
    # Incomplete patterns (regex)
    incomplete_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_INCOMPLETE_PATTERNS))

    # Blocker patterns (regex) - 遇到这些就不续跑
    blocker_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKER_PATTERNS))

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

        # Validate auto approval settings
        if not isinstance(self.auto_approve_permission_requests, bool):
            return False, "auto_approve_permission_requests must be a boolean"

        if not isinstance(self.auto_approve_max_per_session, int) or self.auto_approve_max_per_session < 0:
            return False, "auto_approve_max_per_session must be a non-negative integer"

        if self.auto_approve_max_per_session > 100:
            return False, "auto_approve_max_per_session too large (max: 100)"

        if not isinstance(self.auto_approve_bash, bool):
            return False, "auto_approve_bash must be a boolean"

        if not isinstance(self.auto_approve_tools, list):
            return False, "auto_approve_tools must be a list"
        for tool in self.auto_approve_tools:
            if not isinstance(tool, str) or not tool.strip():
                return False, "auto_approve_tools contains an empty tool name"
            if len(tool.strip()) > 80:
                return False, "auto_approve_tools contains a tool name that is too long"

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
        if "incomplete_patterns" in known_fields:
            known_fields["incomplete_patterns"] = _merge_unique_patterns(
                known_fields.get("incomplete_patterns"),
                DEFAULT_INCOMPLETE_PATTERNS,
            )
        if "blocker_patterns" in known_fields:
            known_fields["blocker_patterns"] = _merge_unique_patterns(
                known_fields.get("blocker_patterns"),
                DEFAULT_BLOCKER_PATTERNS,
            )
        if "auto_approve_tools" in known_fields:
            known_fields["auto_approve_tools"] = _merge_unique_strings(
                known_fields.get("auto_approve_tools"),
                [],
            )
            if known_fields.get("auto_approve_bash", True) and known_fields["auto_approve_tools"]:
                known_fields["auto_approve_tools"] = _merge_unique_strings(
                    ["Bash"],
                    known_fields["auto_approve_tools"],
                )
        elif known_fields.get("auto_approve_bash") is False:
            known_fields["auto_approve_tools"] = [
                tool for tool in DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS
                if tool.casefold() != "bash"
            ]

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
