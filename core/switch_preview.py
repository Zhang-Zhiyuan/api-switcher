"""Build switch previews and static health checks for profile changes."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from config import paths
from core import auth_parser, parser, profile_manager, security, toml_parser, vscode_parser
from models.profile import ClaudeAccountProfile, ClaudeProfile, CodexAccountProfile, CodexProfile


@dataclass(frozen=True)
class PreviewChange:
    label: str
    before: str
    after: str
    note: str = ""
    important: bool = False


@dataclass(frozen=True)
class PreviewCheck:
    category: str
    item: str
    status: str
    message: str
    suggestion: str = ""


@dataclass(frozen=True)
class SwitchPreview:
    kind: str
    target_name: str
    title: str
    summary: str
    changes: list[PreviewChange] = field(default_factory=list)
    checks: list[PreviewCheck] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    @property
    def can_proceed(self) -> bool:
        return not any(check.status == "error" for check in self.checks)

    @property
    def warning_count(self) -> int:
        return sum(1 for check in self.checks if check.status == "warning")

    @property
    def error_count(self) -> int:
        return sum(1 for check in self.checks if check.status == "error")


def build_switch_preview(kind: str, name: str) -> SwitchPreview:
    builders = {
        "claude_api": build_claude_api_preview,
        "codex_api": build_codex_api_preview,
        "claude_account": build_claude_account_preview,
        "codex_account": build_codex_account_preview,
    }
    builder = builders.get(kind)
    if not builder:
        return _missing_preview(kind, name, f"不支持的切换类型: {kind}")
    return builder(name)


def build_claude_api_preview(name: str) -> SwitchPreview:
    target = _find_profile(profile_manager.list_switchable_claude_profiles(), name)
    if not target:
        return _missing_preview("claude_api", name, f"未找到 Claude API 配置: {name}")

    runtime = profile_manager.get_claude_runtime_summary()
    account_runtime = profile_manager.get_claude_account_runtime_summary()
    settings = parser.read_claude_settings()
    env = settings.get("env", {}) if isinstance(settings.get("env"), dict) else {}

    provider_label = _provider_label(target.provider, target.custom_provider_name)
    target_base_url = _claude_target_base_url(target)
    current_profile = _current_label(runtime, "has_settings", "has_config")
    account_before = _account_runtime_label(account_runtime, "has_credentials")
    account_after = "会被第三方 API 配置覆盖"

    changes = [
        PreviewChange("Claude API 配置", current_profile, target.name, important=True),
        PreviewChange("Provider", _display(runtime.get("provider")), provider_label),
        PreviewChange("模型", _display(runtime.get("model")), _display(target.model)),
        PreviewChange("Base URL", _display(env.get("ANTHROPIC_BASE_URL")), _display(target_base_url)),
        PreviewChange("认证", _display(runtime.get("auth_identity")), profile_manager.describe_claude_profile_identity(target)),
        PreviewChange("官方账号状态", account_before, account_after, "切换 API 会清空本应用的官方账号激活记录。", True),
    ]

    checks = _validate_claude_api_target(target)
    checks.extend(_path_checks("Claude 写入路径", [
        parser.CLAUDE_SETTINGS,
        parser.CLAUDE_CONFIG,
        vscode_parser.VSCODE_SETTINGS,
    ]))
    if account_runtime.get("has_credentials"):
        checks.append(PreviewCheck(
            "Claude Code",
            "官方账号覆盖",
            "warning",
            "当前存在 Claude 官方登录凭据，切换第三方 API 后官方账号不会作为运行态生效。",
            "需要恢复官方账号时，切回已保存的官方账号快照。",
        ))
    if current_profile == target.name:
        checks.append(PreviewCheck("Claude Code", "重复切换", "warning", "目标配置已经是当前磁盘配置。"))

    return SwitchPreview(
        kind="claude_api",
        target_name=name,
        title="切换 Claude API 配置",
        summary=f"准备把 Claude Code 切换到第三方 API 配置「{name}」。",
        changes=changes,
        checks=checks,
        files=[
            str(parser.CLAUDE_SETTINGS),
            str(parser.CLAUDE_CONFIG),
            str(vscode_parser.VSCODE_SETTINGS),
        ],
    )


def build_codex_api_preview(name: str) -> SwitchPreview:
    target = _find_profile(profile_manager.list_switchable_codex_profiles(), name)
    if not target:
        return _missing_preview("codex_api", name, f"未找到 Codex API 配置: {name}")

    runtime = profile_manager.get_codex_runtime_summary()
    account_runtime = profile_manager.get_codex_account_runtime_summary()
    config = toml_parser.read_codex_config()
    auth = auth_parser.read_codex_auth()
    target_base_url = _codex_target_base_url(target)
    target_env_key = _codex_target_env_key(target)
    current_profile = _current_label(runtime, "has_config", "has_auth")
    account_before = _account_runtime_label(account_runtime, "has_official_auth")
    account_after = "保留 auth.json；Provider 配置生效"
    auth_after = "OpenAI 认证" if target.custom_requires_openai_auth else f"env_key={target_env_key}"
    auth_note = (
        "requires_openai_auth=true 时 Codex 会忽略 env_key。"
        if target.custom_requires_openai_auth
        else "API Key 写入 provider env_key、Windows 用户环境和 Codex .env。"
    )

    changes = [
        PreviewChange("Codex API 配置", current_profile, target.name, important=True),
        PreviewChange("Provider", _display(runtime.get("provider")), _display(target.model_provider)),
        PreviewChange("模型", _display(runtime.get("model")), _display(target.model)),
        PreviewChange("Base URL", _display(_current_codex_base_url(config)), _display(target_base_url)),
        PreviewChange("认证模式", _display(runtime.get("auth_mode")), auth_after, auth_note, True),
        PreviewChange("认证", _display(runtime.get("auth_identity")), profile_manager.describe_codex_profile_identity(target)),
        PreviewChange("官方账号状态", account_before, account_after, "官方登录 token 会保留；当前 provider 会切到第三方配置。", True),
        PreviewChange("沙盒/审批", f"{runtime.get('approval_policy', '-')}/{runtime.get('sandbox_mode', '-')}",
                      f"{target.approval_policy}/{target.sandbox_mode}"),
    ]
    if "approval_policy" not in runtime or "sandbox_mode" not in runtime:
        changes[-1] = PreviewChange("沙盒/审批", "-", f"{target.approval_policy}/{target.sandbox_mode}")

    checks = _validate_codex_api_target(target)
    checks.extend(_path_checks("Codex 写入路径", [
        toml_parser.CODEX_CONFIG,
        auth_parser.CODEX_AUTH,
        paths.CODEX_ENV,
    ]))
    if _codex_has_official_tokens(auth):
        checks.append(PreviewCheck(
            "Codex CLI",
            "官方账号保留",
            "ok",
            "当前 auth.json 存在 ChatGPT 登录 token；切换第三方 Provider 时会保留它。",
            "第三方 API Key 不再写入 auth.json。",
        ))
    if target.sandbox_mode == "danger-full-access":
        checks.append(PreviewCheck(
            "Codex CLI",
            "沙盒权限",
            "warning",
            "目标配置使用 danger-full-access。",
            "只在可信项目中使用该配置。",
        ))
    if current_profile == target.name:
        checks.append(PreviewCheck("Codex CLI", "重复切换", "warning", "目标配置已经是当前磁盘配置。"))

    return SwitchPreview(
        kind="codex_api",
        target_name=name,
        title="切换 Codex API 配置",
        summary=f"准备把 Codex CLI 切换到第三方 API 配置「{name}」。",
        changes=changes,
        checks=checks,
        files=[str(toml_parser.CODEX_CONFIG), str(auth_parser.CODEX_AUTH), str(paths.CODEX_ENV)],
    )


def build_claude_account_preview(name: str) -> SwitchPreview:
    target = _find_profile(profile_manager.list_claude_account_profiles(), name)
    if not target:
        return _missing_preview("claude_account", name, f"未找到 Claude 官方账号: {name}")

    runtime = profile_manager.get_claude_runtime_summary()
    account_runtime = profile_manager.get_claude_account_runtime_summary()
    current_api = _current_label(runtime, "has_settings", "has_config")
    current_account = _account_runtime_label(account_runtime, "has_credentials")

    changes = [
        PreviewChange("Claude 官方账号", current_account, target.name, important=True),
        PreviewChange("账号身份", _display(account_runtime.get("identity")), _display(target.identity)),
        PreviewChange("第三方 API 状态", current_api, "会清理 API Key/Base URL 覆盖", "切回官方账号后新终端会话生效。", True),
        PreviewChange("模型兜底", _display(runtime.get("model")), "保留 Claude 官方模型和别名；非 Claude 模型会重置为 claude-sonnet-4"),
    ]

    checks = _validate_claude_account_target(target)
    checks.extend(_path_checks("Claude 写入路径", [parser.CLAUDE_CREDENTIALS, parser.CLAUDE_SETTINGS, parser.CLAUDE_CONFIG]))
    if runtime.get("profile_name") or account_runtime.get("api_override_active"):
        checks.append(PreviewCheck(
            "Claude Code",
            "API 覆盖清理",
            "warning",
            "切换官方账号会移除 Claude settings/config 中的第三方 API 覆盖。",
            "如果还需要该 API 配置，可稍后从 API 配置列表切回。",
        ))

    return SwitchPreview(
        kind="claude_account",
        target_name=name,
        title="切换 Claude 官方账号",
        summary=f"准备恢复 Claude Code 官方账号快照「{name}」。",
        changes=changes,
        checks=checks,
        files=[str(parser.CLAUDE_CREDENTIALS), str(parser.CLAUDE_SETTINGS), str(parser.CLAUDE_CONFIG)],
    )


def build_codex_account_preview(name: str) -> SwitchPreview:
    target = _find_profile(profile_manager.list_codex_account_profiles(), name)
    if not target:
        return _missing_preview("codex_account", name, f"未找到 Codex 官方账号: {name}")

    runtime = profile_manager.get_codex_runtime_summary()
    account_runtime = profile_manager.get_codex_account_runtime_summary()
    current_api = _current_label(runtime, "has_config", "has_auth")
    current_account = _account_runtime_label(account_runtime, "has_official_auth")

    changes = [
        PreviewChange("Codex 官方账号", current_account, target.name, important=True),
        PreviewChange("账号身份", _display(account_runtime.get("identity")), _display(target.identity)),
        PreviewChange("第三方 API 状态", current_api, "会切回 openai/file 登录", "切回官方账号后新终端会话生效。", True),
        PreviewChange("认证模式", _display(runtime.get("auth_mode")), "chatgpt/file"),
        PreviewChange("Provider", _display(runtime.get("provider")), "openai"),
    ]

    checks = _validate_codex_account_target(target)
    checks.extend(_path_checks("Codex 写入路径", [auth_parser.CODEX_AUTH, toml_parser.CODEX_CONFIG, paths.CODEX_ENV]))
    if runtime.get("profile_name") or account_runtime.get("api_override_active"):
        checks.append(PreviewCheck(
            "Codex CLI",
            "API 覆盖清理",
            "warning",
            "切换官方账号会清理第三方 Provider 环境变量，并把 provider 设回 openai。",
            "如果还需要该 API 配置，可稍后从 API 配置列表切回。",
        ))

    return SwitchPreview(
        kind="codex_account",
        target_name=name,
        title="切换 Codex 官方账号",
        summary=f"准备恢复 Codex ChatGPT 官方账号快照「{name}」。",
        changes=changes,
        checks=checks,
        files=[str(auth_parser.CODEX_AUTH), str(toml_parser.CODEX_CONFIG)],
    )


def collect_static_health_checks(scope: str | None = None) -> list[PreviewCheck]:
    checks: list[PreviewCheck] = []
    if scope in {None, "claude"}:
        for profile in profile_manager.list_switchable_claude_profiles():
            checks.extend(_prefix_items(_validate_claude_api_target(profile), f"API: {profile.name}"))
        for account in profile_manager.list_claude_account_profiles():
            checks.extend(_prefix_items(_validate_claude_account_target(account), f"账号: {account.name}"))
        account_runtime = profile_manager.get_claude_account_runtime_summary()
        if account_runtime.get("has_credentials") and account_runtime.get("api_override_active"):
            checks.append(PreviewCheck(
                "Claude Code",
                "运行态覆盖",
                "warning",
                "当前存在 Claude 官方登录凭据，但第三方 API 覆盖正在生效。",
                "这是允许的；切回官方账号快照即可恢复官方账号运行态。",
            ))
    if scope in {None, "codex"}:
        for profile in profile_manager.list_switchable_codex_profiles():
            checks.extend(_prefix_items(_validate_codex_api_target(profile), f"API: {profile.name}"))
        for account in profile_manager.list_codex_account_profiles():
            checks.extend(_prefix_items(_validate_codex_account_target(account), f"账号: {account.name}"))
        account_runtime = profile_manager.get_codex_account_runtime_summary()
        if account_runtime.get("has_official_auth") and account_runtime.get("api_override_active"):
            checks.append(PreviewCheck(
                "Codex CLI",
                "运行态覆盖",
                "warning",
                "当前存在 ChatGPT 登录 token，但第三方 API 配置正在覆盖官方账号运行态。",
                "这是允许的；切回官方账号快照即可恢复官方账号运行态。",
            ))
    return checks


def _validate_claude_api_target(profile: ClaudeProfile) -> list[PreviewCheck]:
    checks: list[PreviewCheck] = []
    category = "Claude Code"
    if not profile_manager.is_third_party_claude_profile(profile):
        checks.append(PreviewCheck(category, "配置类型", "error", "目标不是第三方 Claude API 配置。"))
    else:
        checks.append(PreviewCheck(category, "配置类型", "ok", "第三方 Claude API 配置。"))

    token = security.get_secret(profile.auth_token_ref) or security.get_secret(getattr(profile, "primary_api_key_ref", None))
    if token:
        checks.append(PreviewCheck(category, "API 密钥", "ok", "已找到本机保存的 Auth Token/API Key。"))
    else:
        checks.append(PreviewCheck(category, "API 密钥", "error", "未找到本机保存的 Auth Token/API Key。", "编辑该 API 配置并重新保存密钥。"))

    if profile.model:
        checks.append(PreviewCheck(category, "模型", "ok", profile.model))
    else:
        checks.append(PreviewCheck(category, "模型", "error", "模型为空。", "编辑配置并选择模型。"))

    base_url = _claude_target_base_url(profile)
    if _valid_http_url(base_url):
        checks.append(PreviewCheck(category, "Base URL", "ok", base_url))
    else:
        checks.append(PreviewCheck(category, "Base URL", "error", f"Base URL 无效: {_display(base_url)}", "填写 http/https 开头的 API 地址。"))

    try:
        from core.providers import ProviderRegistry

        provider = ProviderRegistry.get_provider(profile.provider)
        if provider and not provider.claude_supported:
            checks.append(PreviewCheck(category, "Provider 支持", "error", f"{provider.display_name} 不支持 Claude Code。"))
        elif provider:
            checks.append(PreviewCheck(category, "Provider 支持", "ok", provider.display_name))
        elif profile.provider != "custom":
            checks.append(PreviewCheck(category, "Provider 支持", "warning", f"未知 Provider: {profile.provider}", "确认它是 Anthropic-compatible。"))
    except Exception as exc:
        checks.append(PreviewCheck(category, "Provider 支持", "warning", f"检查失败: {exc}"))

    return checks


def _validate_codex_api_target(profile: CodexProfile) -> list[PreviewCheck]:
    checks: list[PreviewCheck] = []
    category = "Codex CLI"
    if not profile_manager.is_third_party_codex_profile(profile):
        checks.append(PreviewCheck(category, "配置类型", "error", "目标不是第三方 Codex API 配置。"))
    else:
        checks.append(PreviewCheck(category, "配置类型", "ok", "第三方 Codex API 配置。"))

    if profile.custom_requires_openai_auth:
        checks.append(PreviewCheck(category, "API Key", "ok", "该 Provider 使用 OpenAI 认证，不需要单独 API Key。"))
    elif security.get_secret(profile.api_key_ref):
        checks.append(PreviewCheck(category, "API Key", "ok", "已找到本机保存的 API Key。"))
    else:
        checks.append(PreviewCheck(category, "API Key", "error", "未找到本机保存的 API Key。", "编辑该 API 配置并重新保存密钥。"))

    if profile.model:
        checks.append(PreviewCheck(category, "模型", "ok", profile.model))
    else:
        checks.append(PreviewCheck(category, "模型", "error", "模型为空。", "编辑配置并选择模型。"))

    base_url = _codex_target_base_url(profile)
    if _valid_http_url(base_url):
        checks.append(PreviewCheck(category, "Base URL", "ok", base_url))
    else:
        checks.append(PreviewCheck(category, "Base URL", "error", f"Base URL 无效: {_display(base_url)}", "填写 http/https 开头的 OpenAI-compatible 地址。"))

    try:
        from core.providers import ProviderRegistry

        provider = ProviderRegistry.get_provider(profile.model_provider)
        if provider and not provider.codex_supported:
            checks.append(PreviewCheck(category, "Provider 支持", "error", f"{provider.display_name} 不支持 Codex CLI。"))
        elif provider:
            checks.append(PreviewCheck(category, "Provider 支持", "ok", provider.display_name))
        elif profile.model_provider != "custom":
            checks.append(PreviewCheck(category, "Provider 支持", "warning", f"未知 Provider: {profile.model_provider}", "确认 config.toml 中的 provider 表会被正确写入。"))
    except Exception as exc:
        checks.append(PreviewCheck(category, "Provider 支持", "warning", f"检查失败: {exc}"))

    return checks


def _validate_claude_account_target(profile: ClaudeAccountProfile) -> list[PreviewCheck]:
    ok, reason = profile_manager.validate_claude_account_snapshot(profile)
    return [
        PreviewCheck(
            "Claude Code",
            "官方账号快照",
            "ok" if ok else "error",
            reason,
            "" if ok else "重新导入当前 Claude 官方账号快照。",
        )
    ]


def _validate_codex_account_target(profile: CodexAccountProfile) -> list[PreviewCheck]:
    ok, reason = profile_manager.validate_codex_account_snapshot(profile)
    return [
        PreviewCheck(
            "Codex CLI",
            "官方账号快照",
            "ok" if ok else "error",
            reason,
            "" if ok else "重新导入当前 Codex 官方账号快照。",
        )
    ]


def _path_checks(category: str, paths: list[Path]) -> list[PreviewCheck]:
    return [_path_check(category, Path(path)) for path in paths]


def _path_check(category: str, path: Path) -> PreviewCheck:
    try:
        parent = _nearest_existing_parent(path)
        if not parent:
            return PreviewCheck(category, path.name, "error", f"找不到可写父目录: {path}", "检查配置路径是否存在。")
        if os.access(parent, os.W_OK):
            return PreviewCheck(category, path.name, "ok", f"可写: {path}")
        return PreviewCheck(category, path.name, "error", f"不可写: {path}", "检查目录权限。")
    except Exception as exc:
        return PreviewCheck(category, path.name, "warning", f"写入权限检查失败: {exc}")


def _nearest_existing_parent(path: Path) -> Path | None:
    current = Path(path).expanduser()
    if current.exists():
        return current if current.is_dir() else current.parent
    for parent in [current.parent, *current.parents]:
        if parent.exists():
            return parent
    return None


def _find_profile(profiles, name: str):
    return next((profile for profile in profiles if profile.name == name), None)


def _missing_preview(kind: str, name: str, message: str) -> SwitchPreview:
    return SwitchPreview(
        kind=kind,
        target_name=name,
        title="无法预览切换",
        summary=message,
        checks=[PreviewCheck("切换预览", "目标配置", "error", message)],
    )


def _display(value: object, fallback: str = "-") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _current_label(runtime: dict, *presence_keys: str) -> str:
    profile_name = runtime.get("profile_name")
    if profile_name:
        return str(profile_name)
    if any(runtime.get(key) for key in presence_keys):
        return "未匹配已保存配置"
    return "未配置"


def _account_runtime_label(runtime: dict, presence_key: str) -> str:
    profile_name = runtime.get("profile_name")
    if profile_name:
        return str(profile_name)
    if runtime.get(presence_key):
        if runtime.get("api_override_active"):
            return "已有官方登录，但当前被 API 覆盖"
        return "已有官方登录，未匹配已保存快照"
    return "未发现官方登录"


def _provider_label(provider_id: str, custom_name: str | None = None) -> str:
    try:
        from core.providers import ProviderRegistry

        provider = ProviderRegistry.get_provider(provider_id)
        if provider:
            return provider.display_name
    except Exception:
        pass
    return custom_name or provider_id or "-"


def _claude_target_base_url(profile: ClaudeProfile) -> str:
    if profile.base_url:
        return profile.base_url
    try:
        from core.providers import ProviderRegistry

        return ProviderRegistry.get_claude_base_url(profile.provider) or ""
    except Exception:
        return ""


def _codex_target_base_url(profile: CodexProfile) -> str:
    if profile.custom_base_url:
        return profile.custom_base_url
    try:
        from core.providers import ProviderRegistry

        return ProviderRegistry.get_codex_base_url(profile.model_provider) or ""
    except Exception:
        return ""


def _codex_target_env_key(profile: CodexProfile) -> str:
    try:
        from core.providers import ProviderRegistry

        return ProviderRegistry.get_codex_env_key_for_profile(profile) or "-"
    except Exception:
        return profile.custom_env_key or "OPENAI_API_KEY"


def _current_codex_base_url(config: dict) -> str:
    provider = str(config.get("model_provider") or "openai")
    model_providers = config.get("model_providers")
    if isinstance(model_providers, dict):
        custom = model_providers.get(provider)
        if isinstance(custom, dict):
            return str(custom.get("base_url") or "")
    try:
        from core.providers import ProviderRegistry

        return ProviderRegistry.get_codex_base_url(provider) or ""
    except Exception:
        return ""


def _valid_http_url(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _codex_has_official_tokens(auth: dict) -> bool:
    tokens = auth.get("tokens") if isinstance(auth, dict) else None
    return isinstance(tokens, dict) and bool(tokens)


def _prefix_items(checks: list[PreviewCheck], prefix: str) -> list[PreviewCheck]:
    return [
        PreviewCheck(check.category, f"{prefix} / {check.item}", check.status, check.message, check.suggestion)
        for check in checks
        if check.status != "ok"
    ]
