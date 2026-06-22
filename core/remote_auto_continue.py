"""Install and inspect auto-continue hooks on SSH servers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import logging
import posixpath
import shlex
from typing import Any

from core import profile_manager, remote_config, security
from core.auto_continue.error_patterns import CONTENT_LENGTH_PATTERNS, RECOVERABLE_API_ERROR_PATTERNS
from core.auto_continue.manager import auto_continue_manager
from core.auto_continue.permission_rules import (
    apply_managed_permission_rules,
    ask_rules_from_payload,
    conflicting_permission_rules,
    missing_allow_rules,
    permission_rules_from_auto_settings,
    rules_from_payload,
    rules_payload,
)
from core.ssh_manager import ssh_manager
from models.auto_continue import (
    AutoContinueSettings,
    DEFAULT_TRAINING_COMPLETION_PATTERNS,
    DEFAULT_TRAINING_CONTEXT_PATTERNS,
    DEFAULT_TRAINING_CONTINUE_PROMPT,
    DEFAULT_TRAINING_SKIP_PATTERNS,
)

logger = logging.getLogger(__name__)

AUTO_CONTINUE_GUIDANCE = """<!-- BEGIN AUTO CONTINUE GUIDANCE -->
# Auto-Continue Guidance

Before providing your final response, check if the task is truly complete:
- Are there any remaining TODOs or unfinished work?
- Have all tests been run and passed?
- Has verification been completed?
- Are there any follow-up steps mentioned?

If work remains incomplete, continue working on it rather than stopping.
Only stop when you encounter a genuine blocker that requires user input or decision.
<!-- END AUTO CONTINUE GUIDANCE -->
"""

SCRIPT_NAME = "auto_continue_stop.sh"
SCRIPT_MARKERS = ("auto_continue_stop.sh", "auto_continue_stop.ps1")


def _as_bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class RemoteAutoContinuePaths:
    provider_name: str
    config_dir: str
    hooks_dir: str
    settings_path: str
    script_path: str
    state_dir: str
    guidance_path: str
    provider_config_path: str
    permission_rules_path: str
    error_recovery_script_path: str | None = None
    codex_hooks_path: str | None = None


@dataclass
class RemoteAutoContinueStatus:
    provider_name: str
    remote_os: str = "unknown"
    config_dir: str = ""
    script_path: str = ""
    settings_path: str = ""
    enabled: bool = False
    hook_script_exists: bool = False
    hook_registered: bool = False
    guidance_installed: bool = False
    settings_valid: bool = False
    settings_sha256: str = ""
    expected_settings_sha256: str = ""
    settings_matches_expected: bool | None = None
    git_snapshot_enabled: bool = False
    git_snapshot_master_enabled: bool = False
    git_snapshot_on_start_enabled: bool = False
    git_snapshot_on_recovery_enabled: bool = False
    git_auto_push_enabled: bool = False
    training_auto_continue_enabled: bool = False
    permission_auto_approve_enabled: bool = False
    error_recovery_enabled: bool = False
    permission_mode: str = ""
    git_available: bool = False
    runtime_ready: bool = False
    codex_hooks_enabled: bool | None = None
    hook_script_mode: int | None = None
    hook_script_sha256: str = ""
    expected_hook_script_sha256: str = ""
    hook_script_matches_expected: bool | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "Claude" if self.provider_name == "claude" else "Codex"

    @property
    def ready(self) -> bool:
        hook_required = (
            self.enabled
            or self.training_auto_continue_enabled
            or self.git_snapshot_enabled
            or self.permission_auto_approve_enabled
            or self.error_recovery_enabled
        )
        return (
            hook_required
            and not self.issues
            and self.hook_script_exists
            and self.hook_registered
            and self.settings_valid
            and (self.settings_matches_expected is not False)
            and self.runtime_ready
            and (self.codex_hooks_enabled is not False)
            and (self.hook_script_matches_expected is not False)
            and (not self.git_snapshot_enabled or self.git_available)
        )

    def summary(self) -> str:
        state = "正常" if self.ready else "需处理"
        parts = [
            f"{self.label}: {state}",
            f"Stop续跑 {'ON' if self.enabled else 'OFF'}",
            f"训练续跑 {'ON' if self.training_auto_continue_enabled else 'OFF'}",
            f"Git快照 {'ON' if self.git_snapshot_master_enabled else 'OFF'}",
            f"触发 {'对话/消息/Stop' if self.git_snapshot_on_start_enabled else 'OFF'}",
            f"API恢复 {'ON' if self.error_recovery_enabled else 'OFF'}",
        ]
        if self.git_snapshot_on_recovery_enabled:
            parts.append("恢复前快照 ON")
        if self.git_auto_push_enabled:
            parts.append("推送已有 Git remote ON")
        if self.permission_auto_approve_enabled:
            parts.append("权限自动确认 ON")
        script_state = (
            "缺失"
            if not self.hook_script_exists
            else "需更新"
            if self.hook_script_matches_expected is False
            else "最新"
        )
        settings_state = (
            "缺失/无效"
            if not self.settings_valid
            else "需更新"
            if self.settings_matches_expected is False
            else "最新"
        )
        parts.extend([
            f"Git {'可用' if self.git_available else '缺失'}",
            f"Hook {'已注册' if self.hook_registered else '未注册'}",
            f"脚本 {script_state}",
            f"设置 {settings_state}",
        ])
        if self.provider_name == "claude":
            parts.append(f"权限模式 {self.permission_mode or '(未设置)'}")
        if self.provider_name == "codex":
            parts.append(f"codex_hooks {'已开启' if self.codex_hooks_enabled else '未开启'}")
        if self.issues:
            parts.append("问题: " + "；".join(self.issues[:3]))
        return "，".join(parts)


def _normal_provider(provider_name: str) -> str:
    provider = str(provider_name or "").strip().lower()
    if provider in {"claude", "codex"}:
        return provider
    raise ValueError(f"不支持的自动续跑类型: {provider_name}")


def _provider_label(provider_name: str) -> str:
    return "Claude" if provider_name == "claude" else "Codex"


def _find_ssh_profile(ssh_name: str):
    profile = next((p for p in profile_manager.list_ssh_profiles() if p.name == ssh_name), None)
    if not profile:
        raise ValueError(f"未找到 SSH 服务器: {ssh_name}")
    return profile


def _connect(ssh_name: str):
    profile = _find_ssh_profile(ssh_name)
    return profile, ssh_manager.connect(profile)


def _paths(client, ssh_profile, provider_name: str) -> RemoteAutoContinuePaths:
    provider = _normal_provider(provider_name)
    base_dir = remote_config._expand_remote_path(
        client,
        remote_config._remote_dir(ssh_profile, provider),
    )
    hooks_dir = posixpath.join(base_dir, "hooks")
    settings_path = posixpath.join(base_dir, "auto_continue_settings.json")
    script_path = posixpath.join(hooks_dir, SCRIPT_NAME)
    state_dir = posixpath.join(base_dir, "tmp")
    permission_rules_path = posixpath.join(base_dir, "auto_continue_permission_rules.json")
    if provider == "claude":
        guidance_path = posixpath.join(base_dir, "CLAUDE.md")
        provider_config_path = posixpath.join(base_dir, "settings.json")
        codex_hooks_path = None
    else:
        guidance_path = posixpath.join(base_dir, "AGENTS.md")
        provider_config_path = posixpath.join(base_dir, "config.toml")
        codex_hooks_path = posixpath.join(base_dir, "hooks.json")
    return RemoteAutoContinuePaths(
        provider_name=provider,
        config_dir=base_dir,
        hooks_dir=hooks_dir,
        settings_path=settings_path,
        script_path=script_path,
        state_dir=state_dir,
        guidance_path=guidance_path,
        provider_config_path=provider_config_path,
        permission_rules_path=permission_rules_path,
        error_recovery_script_path=script_path,
        codex_hooks_path=codex_hooks_path,
    )


def _remote_file_exists(client, path: str) -> bool:
    sftp = None
    try:
        sftp = client.open_sftp()
        sftp.stat(path)
        return True
    except Exception:
        return False
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass


def _remove_remote_file(client, path: str) -> None:
    sftp = None
    try:
        sftp = client.open_sftp()
        sftp.remove(path)
    except FileNotFoundError:
        return
    except OSError as e:
        if "No such file" not in str(e):
            logger.debug(f"Failed to remove remote file {path}: {e}")
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass


def _read_text(client, path: str) -> str | None:
    return ssh_manager.read_remote_file(client, path)


def _write_text(client, path: str, content: str, mode: int | None = None) -> None:
    ssh_manager.write_remote_file(client, path, content, file_mode=mode)


def _remote_file_mode(client, path: str) -> int | None:
    sftp = None
    try:
        sftp = client.open_sftp()
        return sftp.stat(path).st_mode & 0o777
    except Exception:
        return None
    finally:
        if sftp:
            try:
                sftp.close()
            except Exception:
                pass


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_json(data: Any) -> str:
    return _sha256_text(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _backup_remote_text(client, path: str, content: str | None, reason: str) -> str | None:
    if content is None:
        return None

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    mode = _remote_file_mode(client, path) or 0o600
    for suffix in [""] + [f".{i}" for i in range(1, 100)]:
        backup_path = f"{path}.bak-{timestamp}{suffix}"
        if _remote_file_exists(client, backup_path):
            continue
        try:
            _write_text(client, backup_path, content, mode=mode)
            logger.warning(f"Backed up remote file {path} to {backup_path}: {reason}")
            return backup_path
        except Exception as e:
            logger.warning(f"Failed to back up remote file {path}: {e}")
            return None
    return None


def _snapshot_remote_files(client, paths: list[str]) -> dict[str, tuple[str | None, int | None]]:
    snapshots: dict[str, tuple[str | None, int | None]] = {}
    for path in dict.fromkeys(p for p in paths if p):
        content = _read_text(client, path)
        snapshots[path] = (content, _remote_file_mode(client, path) if content is not None else None)
    return snapshots


def _restore_remote_files(client, snapshots: dict[str, tuple[str | None, int | None]]) -> None:
    for path, (content, mode) in snapshots.items():
        try:
            if content is None:
                _remove_remote_file(client, path)
            else:
                _write_text(client, path, content, mode=mode)
        except Exception as e:
            logger.warning(f"Failed to restore remote file {path}: {e}")


def _read_json(client, path: str, default: Any = None, strict: bool = True) -> Any:
    raw = _read_text(client, path)
    if raw is None or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        if strict:
            raise RuntimeError(f"远端 JSON 解析失败: {path}: {e}") from e
        return default


def _write_json(client, path: str, data: Any, mode: int | None = 0o600) -> None:
    _write_text(client, path, json.dumps(data, indent=2, ensure_ascii=False), mode)


def _read_json_object_for_update(client, path: str, label: str) -> dict:
    raw = _read_text(client, path)
    if raw is None or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _backup_remote_text(client, path, raw, f"{label} invalid JSON: {e}")
        return {}
    if not isinstance(data, dict):
        _backup_remote_text(client, path, raw, f"{label} expected object, got {type(data).__name__}")
        return {}
    return data


def _read_codex_hooks_json_for_update(client, path: str) -> dict:
    return _read_json_object_for_update(client, path, "Codex hooks.json")


def _read_toml(client, path: str, strict: bool = True) -> dict:
    raw = _read_text(client, path)
    if raw is None or not raw.strip():
        return {}
    try:
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        return tomllib.loads(raw)
    except Exception as e:
        if strict:
            raise RuntimeError(f"远端 TOML 解析失败: {path}: {e}") from e
        return {}


def _write_toml(client, path: str, data: dict, mode: int | None = 0o600) -> None:
    import tomli_w

    _write_text(client, path, tomli_w.dumps(data), mode)


def _probe_remote_environment(client) -> dict:
    command = (
        "printf 'os='; (uname -s 2>/dev/null || printf unknown); printf '\\n'; "
        "printf 'sh='; (command -v sh 2>/dev/null || true); printf '\\n'; "
        "printf 'python='; "
        "if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then "
        "command -v python3; "
        "elif command -v python >/dev/null 2>&1 && python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' 2>/dev/null; then "
        "command -v python; "
        "fi; printf '\\n'; "
        "printf 'git='; (command -v git 2>/dev/null || true); printf '\\n'; "
        "printf 'sudo='; (command -v sudo 2>/dev/null || true); printf '\\n'; "
        "printf 'uid='; (id -u 2>/dev/null || printf unknown); printf '\\n'; "
        "printf 'pkg='; "
        "for pm in apt-get dnf yum microdnf apk pacman zypper; do "
        "if command -v \"$pm\" >/dev/null 2>&1; then printf '%s' \"$pm\"; break; fi; "
        "done; printf '\\n'"
    )
    stdout, _stderr = ssh_manager.execute_command(client, command, timeout=10)
    result = {"os": "unknown", "sh": "", "python": "", "git": "", "sudo": "", "uid": "", "pkg": "", "is_posix": False}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    os_name = result.get("os", "unknown").lower()
    result["is_posix"] = any(token in os_name for token in ["linux", "darwin", "freebsd", "openbsd", "netbsd"])
    result["is_root"] = result.get("uid") == "0"
    return result


def _ensure_runtime_ready(env: dict) -> None:
    if not env.get("is_posix"):
        raise RuntimeError(f"远端自动续跑当前仅支持 POSIX/Linux SSH 环境，检测到: {env.get('os') or 'unknown'}")
    if not env.get("sh"):
        raise RuntimeError("远端缺少 sh，无法运行自动续跑 hook")
    if not env.get("python"):
        raise RuntimeError("远端缺少 Python 3.6+，无法运行自动续跑判断逻辑")


def _ensure_runtime_ready_with_git(env: dict, require_git: bool = False) -> None:
    _ensure_runtime_ready(env)
    if require_git and not env.get("git"):
        raise RuntimeError("远端缺少 git，无法创建 Git 快照")


def _sudo_command(client, ssh_profile, env: dict, command: str, timeout: int = 180) -> tuple[int, str, str]:
    if env.get("is_root"):
        return ssh_manager.execute_command_with_status(client, command, timeout=timeout)

    if not env.get("sudo"):
        raise RuntimeError("远端不是 root，且没有 sudo，无法自动安装依赖")

    sudo_cmd = f"sudo -n sh -c {shlex.quote(command)}"
    status, stdout, stderr = ssh_manager.execute_command_with_status(client, sudo_cmd, timeout=timeout)
    if status == 0:
        return status, stdout, stderr

    if getattr(ssh_profile, "auth_type", None) != "password" or not getattr(ssh_profile, "password_ref", None):
        raise RuntimeError("远端需要 sudo 密码，但当前 SSH 配置不是密码登录，无法自动输入 sudo 密码")

    password = security.get_secret(ssh_profile.password_ref)
    if not password:
        raise RuntimeError("无法读取已保存的 SSH 密码，不能自动输入 sudo 密码")

    sudo_cmd = f"sudo -S -p '' sh -c {shlex.quote(command)}"
    return ssh_manager.execute_command_with_status(
        client,
        sudo_cmd,
        timeout=timeout,
        input_data=f"{password}\n",
        log_command=False,
        get_pty=True,
    )


def _install_command_for_packages(package_manager: str, missing: list[str]) -> str:
    wants_git = "git" in missing
    wants_python = "python" in missing

    if package_manager == "apt-get":
        packages = []
        if wants_git:
            packages.append("git")
        if wants_python:
            packages.append("python3")
        return "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y " + " ".join(packages)
    if package_manager in {"dnf", "yum", "microdnf"}:
        packages = []
        if wants_git:
            packages.append("git")
        if wants_python:
            packages.append("python3")
        return f"{package_manager} install -y " + " ".join(packages)
    if package_manager == "apk":
        packages = []
        if wants_git:
            packages.append("git")
        if wants_python:
            packages.append("python3")
        return "apk add --no-cache " + " ".join(packages)
    if package_manager == "pacman":
        packages = []
        if wants_git:
            packages.append("git")
        if wants_python:
            packages.append("python")
        return "pacman -Sy --noconfirm " + " ".join(packages)
    if package_manager == "zypper":
        packages = []
        if wants_git:
            packages.append("git")
        if wants_python:
            packages.append("python3")
        return "zypper --non-interactive install " + " ".join(packages)

    raise RuntimeError(f"不支持自动安装的包管理器: {package_manager or 'unknown'}")


def _ensure_remote_runtime(client, ssh_profile, require_git: bool = False) -> dict:
    env = _probe_remote_environment(client)

    if not env.get("is_posix"):
        _ensure_runtime_ready_with_git(env, require_git=require_git)
    if not env.get("sh"):
        raise RuntimeError("远端缺少 sh。因为 SSH 命令和安装脚本都需要 shell，无法自动安装 sh")

    missing = []
    if not env.get("python"):
        missing.append("python")
    if require_git and not env.get("git"):
        missing.append("git")

    if missing:
        package_manager = env.get("pkg") or ""
        if not package_manager:
            raise RuntimeError(f"远端缺少 {', '.join(missing)}，但未检测到支持的包管理器，无法自动安装")

        install_cmd = _install_command_for_packages(package_manager, missing)
        status, _stdout, stderr = _sudo_command(client, ssh_profile, env, install_cmd, timeout=300)
        if status != 0:
            raise RuntimeError(f"自动安装依赖失败: {stderr.strip() or 'unknown error'}")

        env = _probe_remote_environment(client)

    _ensure_runtime_ready_with_git(env, require_git=require_git)
    return env


def _load_local_settings(provider_name: str, settings: AutoContinueSettings | None = None) -> AutoContinueSettings:
    provider = _normal_provider(provider_name)
    source = settings or auto_continue_manager.get_settings(provider) or AutoContinueSettings()
    copied = AutoContinueSettings.from_dict(source.to_dict())
    copied.enabled = True
    if provider == "codex":
        copied.apply_to_subagents = False
    valid, error = copied.validate()
    if not valid:
        raise ValueError(f"自动续跑设置无效: {error}")
    return copied


def _load_git_snapshot_settings(provider_name: str, settings: AutoContinueSettings | None = None) -> AutoContinueSettings:
    provider = _normal_provider(provider_name)
    source = settings or auto_continue_manager.get_settings(provider) or AutoContinueSettings()
    copied = AutoContinueSettings.from_dict(source.to_dict())
    copied.enabled = False
    copied.training_auto_continue_enabled = False
    copied.git_auto_snapshot = True
    copied.git_snapshot_on_start = True
    copied.error_recovery_enabled = False
    copied.auto_approve_permission_requests = False
    if provider == "codex":
        copied.apply_to_subagents = False
    valid, error = copied.validate()
    if not valid:
        raise ValueError(f"Git snapshot settings invalid: {error}")
    return copied


def _settings_require_remote_hook(provider_name: str, settings: AutoContinueSettings | None) -> bool:
    if not settings:
        return False
    provider = _normal_provider(provider_name)
    return bool(
        settings.enabled
        or settings.training_auto_continue_enabled
        or (settings.git_auto_snapshot and settings.git_snapshot_on_start)
        or settings.error_recovery_enabled
        or (provider == "claude" and settings.auto_approve_permission_requests)
    )


def _settings_require_remote_git(settings: AutoContinueSettings | None) -> bool:
    if not settings:
        return False
    return bool(
        settings.git_auto_snapshot
        and (
            settings.git_snapshot_on_start
            or (settings.error_recovery_enabled and settings.git_snapshot_on_recovery)
        )
    )


def _remote_switch_baseline_settings(provider_name: str) -> AutoContinueSettings:
    provider = _normal_provider(provider_name)
    source = auto_continue_manager.get_settings(provider) or AutoContinueSettings()
    copied = AutoContinueSettings.from_dict(source.to_dict())
    copied.enabled = False
    copied.training_auto_continue_enabled = False
    copied.git_auto_snapshot = True
    copied.git_snapshot_on_start = True
    copied.git_snapshot_on_recovery = True
    copied.error_recovery_enabled = False
    copied.auto_approve_permission_requests = False
    if provider == "codex":
        copied.apply_to_subagents = False
    return copied


def _load_remote_settings_for_update(client, paths: RemoteAutoContinuePaths, provider_name: str) -> AutoContinueSettings:
    settings = _read_json(client, paths.settings_path, default=None, strict=False)
    if isinstance(settings, dict):
        try:
            parsed = AutoContinueSettings.from_dict(settings)
            valid, _error = parsed.validate()
            if valid:
                return parsed
        except Exception as e:
            logger.warning(f"Remote auto-continue settings are invalid and will be rebuilt: {e}")
    return _remote_switch_baseline_settings(provider_name)


def _python_literal_list(values: list[str]) -> str:
    lines = ["["]
    for value in values:
        if '"' not in value and not value.endswith("\\"):
            literal = f'r"{value}"'
        else:
            literal = repr(value)
        lines.append(f"    {literal},")
    lines.append("]")
    return "\n".join(lines)


def _generate_remote_hook_script(settings_path: str, state_dir: str) -> str:
    header = "\n".join(
        [
            "#!/bin/sh",
            "# Auto-continue hook script for remote POSIX servers.",
            "# Generated by API Switcher.",
            f"SETTINGS_PATH={shlex.quote(settings_path)}",
            f"STATE_DIR={shlex.quote(state_dir)}",
            'mkdir -p "$STATE_DIR" 2>/dev/null || true',
            'INPUT_PATH="$STATE_DIR/auto_continue_input_$$.json"',
            'cat > "$INPUT_PATH" 2>/dev/null || true',
            'PYTHON_BIN=""',
            'if command -v python3 >/dev/null 2>&1 && python3 -c \'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)\' 2>/dev/null; then',
            '  PYTHON_BIN="$(command -v python3)"',
            'elif command -v python >/dev/null 2>&1 && python -c \'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)\' 2>/dev/null; then',
            '  PYTHON_BIN="$(command -v python)"',
            "fi",
            'if [ -n "$PYTHON_BIN" ]; then',
            '  "$PYTHON_BIN" - "$SETTINGS_PATH" "$STATE_DIR" "$INPUT_PATH" <<\'PY\'',
        ]
    )
    body = r'''
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse


DEFAULT_GITIGNORE_LINES = [
    "# Python",
    "__pycache__/",
    "*.py[cod]",
    "build/",
    "dist/",
    ".venv/",
    "venv/",
    "env/",
    "",
    "# Dependency caches / generated output",
    "node_modules/",
    ".next/",
    ".nuxt/",
    "target/",
    ".cache/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "coverage/",
    ".coverage",
    "",
    "# Local secrets",
    ".env",
    ".env.*",
    "!.env.example",
    "!.env.sample",
    "",
    "# Logs",
    "*.log",
    "logs/",
]


def log(message, level="INFO"):
    ts = datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat()
    print(f"{ts} [{level}] {message}", file=sys.stderr)


def as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def as_int(value, default):
    try:
        number = int(value)
    except Exception:
        return default
    return number if number >= 0 else default


def flatten_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (flatten_text(v) for v in value) if part)
    if isinstance(value, dict):
        for key in ("text", "content", "message", "body"):
            text = flatten_text(value.get(key))
            if text:
                return text
        return "\n".join(part for part in (flatten_text(v) for v in value.values()) if part)
    return str(value)


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def pick_text(data, keys):
    for key in keys:
        text = flatten_text(data.get(key))
        if text.strip():
            return text
    return ""


def transcript_tail(path):
    if not path or not isinstance(path, str) or not os.path.exists(path):
        return ""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(size - 131072, 0))
            raw = handle.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log(f"Failed to read transcript: {exc}", "WARN")
        return ""

    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        message = obj.get("message") if isinstance(obj, dict) else None
        role = ""
        if isinstance(message, dict):
            role = str(message.get("role") or "")
            content = message.get("content")
        elif isinstance(obj, dict):
            role = str(obj.get("role") or "")
            content = obj.get("content") or obj.get("text") or obj
        else:
            continue
        if role and role != "assistant":
            continue
        text = flatten_text(content)
        if text.strip():
            return text
    return ""


def matching_pattern(patterns, text):
    if not isinstance(patterns, list):
        return ""
    for pattern in patterns:
        pattern_text = str(pattern or "").strip()
        if not pattern_text:
            continue
        try:
            if re.search(pattern_text, text, re.IGNORECASE):
                return pattern_text
        except re.error as exc:
            log(f"Invalid pattern ignored: {pattern}: {exc}", "WARN")
    return ""


RECOVERABLE_API_ERROR_PATTERNS = __RECOVERABLE_API_ERROR_PATTERNS__
CONTENT_LENGTH_PATTERNS = __CONTENT_LENGTH_PATTERNS__


DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS = ["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"]
PROMPT_SNAPSHOT_EVENTS = {"UserPromptSubmit", "SessionStart"}
STOP_SNAPSHOT_EVENTS = {"Stop", "SubagentStop"}
TRAINING_COMPLETION_PATTERNS = __TRAINING_COMPLETION_PATTERNS__
TRAINING_SKIP_PATTERNS = __TRAINING_SKIP_PATTERNS__
TRAINING_CONTEXT_PATTERNS = __TRAINING_CONTEXT_PATTERNS__
DEFAULT_TRAINING_CONTINUE_PROMPT = __DEFAULT_TRAINING_CONTINUE_PROMPT__


def is_recoverable_api_error(text):
    for pattern in RECOVERABLE_API_ERROR_PATTERNS:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error as exc:
            log(f"Invalid recoverable API error pattern ignored: {pattern}: {exc}", "WARN")
    return False


def first_text(obj, keys):
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        if key not in obj:
            continue
        text = flatten_text(obj.get(key))
        if text.strip():
            return text
    return ""


def header_value(headers, keys):
    if not isinstance(headers, dict):
        return ""
    lower_map = {str(key).lower(): value for key, value in headers.items()}
    for key in keys:
        value = lower_map.get(str(key).lower())
        text = flatten_text(value)
        if text.strip():
            return text
    return ""


def parse_int(value, default=0):
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(str(value).strip())
    except Exception:
        return default


def int_setting(settings, name, default, minimum, maximum):
    value = parse_int(settings.get(name), default)
    if value < minimum:
        value = minimum
    if value > maximum:
        value = maximum
    return value


def clamped_seconds(value, default=60, maximum=600):
    seconds = parse_int(value, default)
    if seconds < 1:
        seconds = default
    if seconds < 1:
        seconds = 1
    return min(seconds, maximum)


def retry_after_seconds(error_message="", retry_after_text="", default=60, maximum=600):
    for candidate in (retry_after_text, error_message):
        text = str(candidate or "").strip()
        if not text:
            continue
        if re.fullmatch(r"\d+", text):
            return clamped_seconds(text, default, maximum)
        match = re.search(r"(\d+)\s*(ms|millisecond|milliseconds)\b", text, re.IGNORECASE)
        if match:
            seconds = int((int(match.group(1)) + 999) / 1000)
            return clamped_seconds(seconds, default, maximum)
        match = re.search(r"(\d+)\s*(s|sec|secs|second|seconds|秒)(?:\b|\s|后|$)", text, re.IGNORECASE)
        if match:
            return clamped_seconds(match.group(1), default, maximum)
        match = re.search(r"(\d+)\s*(m|min|mins|minute|minutes|分钟)(?:\b|\s|后|$)", text, re.IGNORECASE)
        if match:
            return clamped_seconds(int(match.group(1)) * 60, default, maximum)
        match = re.search(
            r"(retry|try again|wait|重试|等待|稍后).{0,80}?(\d+)\s*(s|sec|secs|second|seconds|秒)?(?:\b|\s|后|$)",
            text,
            re.IGNORECASE,
        )
        if match:
            return clamped_seconds(match.group(2), default, maximum)
        try:
            from email.utils import parsedate_to_datetime

            retry_at = parsedate_to_datetime(text)
            if retry_at is not None:
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                seconds = int((retry_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
                return clamped_seconds(seconds, default, maximum)
        except Exception:
            pass
    return clamped_seconds(default, default, maximum)


def backoff_seconds(attempt, initial_delay, max_delay):
    attempt = max(1, parse_int(attempt, 1))
    seconds = initial_delay * (2 ** (attempt - 1))
    return int(min(seconds, max_delay))


def extract_error_fields(data):
    error_code = first_text(data, ["error_code", "code", "errorCode", "error_type", "type"])
    error_message = first_text(
        data,
        [
            "error_message",
            "message",
            "error",
            "errorMessage",
            "hint",
            "detail",
            "response",
            "body",
            "data",
            "errors",
            "stderr",
            "stdout",
        ],
    )
    http_status = parse_int(first_text(data, ["status", "http_status", "status_code", "statusCode"]), 0)
    retry_after = first_text(data, ["retry_after", "retryAfter", "retry_after_seconds", "retryAfterSeconds", "Retry-After"])
    header_retry_after = header_value(data.get("headers"), ["retry-after", "Retry-After", "retry_after", "retryAfter"])
    if header_retry_after:
        retry_after = header_retry_after

    nested = data.get("error")
    if isinstance(nested, dict):
        nested_code = first_text(nested, ["code", "type", "error_code", "errorCode"])
        nested_message = first_text(nested, ["message", "error_message", "errorMessage", "detail", "hint", "response", "body", "data", "errors"])
        nested_status = first_text(nested, ["status", "status_code", "statusCode", "http_status"])
        nested_retry_after = first_text(nested, ["retry_after", "retryAfter", "retry_after_seconds", "retryAfterSeconds", "Retry-After"])
        nested_header_retry_after = header_value(nested.get("headers"), ["retry-after", "Retry-After", "retry_after", "retryAfter"])
        if nested_code:
            error_code = nested_code
        if nested_message:
            error_message = nested_message
        if nested_status:
            http_status = parse_int(nested_status, http_status)
        if nested_retry_after:
            retry_after = nested_retry_after
        if nested_header_retry_after:
            retry_after = nested_header_retry_after

    return error_code, error_message, http_status, retry_after


def classify_api_error(error_code, error_message, http_status):
    combined = f"{error_code or ''} {error_message or ''}".lower()
    for pattern in CONTENT_LENGTH_PATTERNS:
        try:
            if re.search(pattern, combined, re.IGNORECASE):
                return "content_length"
        except re.error as exc:
            log(f"Invalid content-length pattern ignored: {pattern}: {exc}", "WARN")

    if http_status == 429 or re.search(r"rate.*limit|too.*many|retry.*after|请求.*频繁|速率|频率", combined, re.IGNORECASE):
        return "rate_limit"
    if http_status == 401 or re.search(r"authentication.*failed|invalid.*api.*key|unauthorized|auth|认证|密钥", combined, re.IGNORECASE):
        return "auth"
    if http_status == 403 or re.search(r"permission.*denied|access.*denied|forbidden|权限", combined, re.IGNORECASE):
        return "permission"
    if re.search(r"quota|insufficient.*balance|insufficient.*quota|配额|余额", combined, re.IGNORECASE):
        return "quota"
    if http_status == 504 or re.search(r"timeout|timed.*out|request timed out|请求.*超时|超时", combined, re.IGNORECASE):
        return "timeout"
    if is_recoverable_api_error(combined):
        return "network"
    if http_status == 503 or re.search(r"overload|capacity.*exceeded|service unavailable|503|繁忙|过载", combined, re.IGNORECASE):
        return "overload"
    if 500 <= http_status < 600:
        return "server"
    if 400 <= http_status < 500:
        return "invalid"
    return "unknown"


def error_recovery_output(is_claude, error_type, wait_seconds, compact_transport, error_message):
    if error_type == "content_length":
        if is_claude:
            return {
                "decision": "recover",
                "commands": [
                    {"type": "slash_command", "command": "compact"},
                    {"type": "user_message", "message": "继续"},
                ],
                "suppressOutput": True,
                "userMessage": "对话内容过长，正在自动压缩并继续...",
            }
        return {
            "decision": "recover",
            "recover": True,
            "commands": [
                {"type": "slash_command", "command": "compact"},
                {"type": "user_message", "message": "继续"},
            ],
            "suppressOutput": True,
            "userMessage": "对话内容过长，正在自动压缩并继续；如果压缩失败会自动重试直到成功...",
        }

    if error_type == "rate_limit":
        if is_claude:
            return {
                "decision": "recover",
                "commands": [
                    {"type": "wait", "seconds": wait_seconds},
                    {"type": "user_message", "message": "继续"},
                ],
                "suppressOutput": True,
                "userMessage": f"请求过于频繁，等待 {wait_seconds} 秒后重试...",
            }
        return {"recover": True, "wait": wait_seconds, "commands": ["继续"], "userMessage": f"请求过于频繁，等待 {wait_seconds} 秒后重试..."}

    if error_type in {"timeout", "overload", "network", "server"}:
        if is_claude:
            commands = [{"type": "wait", "seconds": wait_seconds}]
            if compact_transport:
                commands.append({"type": "slash_command", "command": "compact"})
            commands.append({"type": "user_message", "message": "继续"})
            return {
                "decision": "recover",
                "commands": commands,
                "suppressOutput": True,
                "userMessage": f"服务暂时不可用，等待 {wait_seconds} 秒后重试...",
            }
        if compact_transport:
            commands = [
                {"type": "slash_command", "command": "compact"},
                {"type": "user_message", "message": "继续"},
            ]
            user_message = f"压缩任务连接中断，等待 {wait_seconds} 秒后重新压缩并继续；会自动重试直到成功..."
        else:
            commands = ["继续"]
            user_message = f"服务暂时不可用，等待 {wait_seconds} 秒后重试..."
        return {
            "decision": "recover",
            "recover": True,
            "wait": wait_seconds,
            "commands": commands,
            "suppressOutput": True,
            "userMessage": user_message,
        }

    if error_type in {"auth", "permission", "quota"}:
        message = {
            "auth": "认证失败，请检查 API 密钥",
            "permission": "权限不足，请检查账户权限",
            "quota": "配额已用完，请充值或等待配额重置",
        }.get(error_type, f"发生错误: {error_message}")
        if is_claude:
            return {"decision": "notify", "userMessage": message, "suppressOutput": False}
        return {"recover": False, "notify": True, "userMessage": message}

    return None


def handle_error_recovery(data, settings, state_dir, is_claude, session_id):
    if not as_bool(settings.get("error_recovery_enabled"), False):
        return False

    error_code, error_message, http_status, retry_after_text = extract_error_fields(data)
    if not str(error_code or "").strip() and not str(error_message or "").strip():
        return False

    error_type = classify_api_error(error_code, error_message, http_status)
    if error_type in {"invalid", "unknown"}:
        return False

    compact_source = f"{error_code or ''} {error_message or ''}"
    compact_transport = bool(re.search(r"remote compact task|backend-api/codex/responses/compact|responses/compact", compact_source, re.IGNORECASE))
    compact_recovery = (not is_claude) and (error_type == "content_length" or compact_transport)

    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "error_recovery_state.json")
    log_path = os.path.join(state_dir, "error_recovery_log.jsonl")
    state_seed = f"{session_id}|{error_type}"
    state_key = hashlib.sha256(state_seed.encode("utf-8", errors="replace")).hexdigest()
    max_recoveries = int_setting(settings, "max_error_recoveries", 3, 0, 10)
    lock_path = state_path + ".lock"
    lock_fd = None
    for _ in range(20):
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 60:
                    os.unlink(lock_path)
                    continue
            except FileNotFoundError:
                continue
            except Exception:
                pass
            time.sleep(0.1)
    if lock_fd is None:
        log("Failed to acquire error recovery state lock", "WARN")
        return True

    try:
        state = load_state(state_path)
        recovery_count = as_int(state.get(state_key), 0)

        if not compact_recovery and recovery_count >= max_recoveries:
            write_jsonl(log_path, {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "session_id": str(session_id),
                "error_type": error_type,
                "error_code": error_code,
                "error_message": error_message,
                "http_status": http_status,
                "action": "max_recoveries_reached",
                "recovery_count": recovery_count,
            })
            return True

        recovery_count += 1
        state[state_key] = recovery_count
        save_state(state_path, state)
        if compact_recovery and recovery_count > max_recoveries:
            log(f"Compact recovery is retry-until-success; ignoring max_error_recoveries={max_recoveries}", "INFO")
    finally:
        try:
            if lock_fd is not None:
                os.close(lock_fd)
        finally:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            except Exception:
                pass

    git_commit_hash = ""
    if as_bool(settings.get("git_auto_snapshot"), True) and as_bool(settings.get("git_snapshot_on_recovery"), True):
        git_commit_hash = run_git_snapshot(as_bool(settings.get("git_auto_push"), False))

    log_entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": str(session_id),
        "error_type": error_type,
        "error_code": error_code,
        "error_message": error_message,
        "http_status": http_status,
        "action": "attempting_recovery",
        "recovery_count": recovery_count,
    }
    if git_commit_hash:
        log_entry["git_commit_hash"] = git_commit_hash
    write_jsonl(log_path, log_entry)

    retry_initial = int_setting(settings, "error_retry_initial_delay_seconds", 5, 1, 300)
    retry_max = int_setting(settings, "error_retry_max_delay_seconds", 60, 1, 600)
    if retry_initial > retry_max:
        retry_initial = retry_max

    wait_seconds = 0
    if error_type == "rate_limit":
        wait_seconds = retry_after_seconds(error_message, retry_after_text, 60, 600)
    elif error_type in {"timeout", "overload", "network", "server"}:
        wait_seconds = backoff_seconds(recovery_count, retry_initial, retry_max)

    output = error_recovery_output(is_claude, error_type, wait_seconds, compact_transport, error_message)
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return True


def permission_tools(settings):
    settings = settings if isinstance(settings, dict) else {}
    legacy_bash_allowed = as_bool(settings.get("auto_approve_bash"), True)
    if "auto_approve_tools" in settings:
        allowed_tools = settings.get("auto_approve_tools")
        tools = allowed_tools if isinstance(allowed_tools, list) else []
    elif legacy_bash_allowed:
        tools = DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS
    else:
        tools = [tool for tool in DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS if tool.lower() != "bash"]
    result = []
    seen = set()
    for item in tools:
        value = str(item or "").strip()
        key = value.lower()
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    if legacy_bash_allowed and "auto_approve_tools" in settings and result and "bash" not in seen:
        result.insert(0, "Bash")
    return result


def tool_allowed(tool_name, allowed_tools):
    if not tool_name:
        return False
    tools = allowed_tools if isinstance(allowed_tools, list) else []
    for item in tools:
        allowed = str(item or "").strip()
        if not allowed:
            continue
        rule_tool = allowed.split("(", 1)[0].strip() if "(" in allowed else allowed
        if allowed == "*" or allowed.lower() == tool_name.lower() or rule_tool.lower() == tool_name.lower():
            return True
        if "*" in allowed:
            pattern = "^" + re.escape(allowed).replace("\\*", ".*") + "$"
            try:
                if re.match(pattern, tool_name, re.IGNORECASE):
                    return True
            except re.error:
                continue
    return False


PROJECT_DIR_FIELDS = (
    "cwd",
    "uri",
    "current_directory",
    "currentDirectory",
    "current_working_directory",
    "currentWorkingDirectory",
    "workspace",
    "workspace_dir",
    "workspaceDir",
    "workspace_folders",
    "workspaceFolders",
    "project_dir",
    "projectDir",
    "project_path",
    "projectPath",
    "project_root",
    "projectRoot",
    "repo_path",
    "repoPath",
    "repo_root",
    "repoRoot",
    "repository_path",
    "repositoryPath",
    "repository_root",
    "repositoryRoot",
    "root",
    "root_dir",
    "rootDir",
    "root_path",
    "rootPath",
)


def add_project_dir_candidate(candidates, value):
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip().strip('"')
        if text:
            candidates.append(text)
        return
    if isinstance(value, dict):
        for key in PROJECT_DIR_FIELDS + ("path", "dir", "directory"):
            if key in value:
                add_project_dir_candidate(candidates, value.get(key))
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            add_project_dir_candidate(candidates, item)


def resolve_hook_project_dir(data):
    candidates = []
    for key in PROJECT_DIR_FIELDS:
        add_project_dir_candidate(candidates, data.get(key))
    for key in ("workspace", "project", "repository", "repo", "context"):
        add_project_dir_candidate(candidates, data.get(key))

    seen = set()
    for candidate in candidates:
        expanded = normalize_project_dir_candidate(candidate)
        if not expanded:
            continue
        if expanded in seen:
            continue
        seen.add(expanded)
        if os.path.isdir(expanded):
            return expanded
    return ""


def normalize_project_dir_candidate(candidate):
    text = str(candidate or "").strip().strip('"').strip("'")
    if not text:
        return ""
    if text.lower().startswith("file://"):
        try:
            parsed = urllib.parse.urlparse(text)
            if parsed.scheme.lower() != "file":
                return ""
            text = urllib.parse.unquote(parsed.path or "")
            if os.name == "nt" and re.match(r"^/[A-Za-z]:/", text):
                text = text[1:]
        except Exception as exc:
            log(f"Invalid file URI hook project directory candidate ignored: {exc}", "WARN")
            return ""
    return os.path.abspath(os.path.expandvars(os.path.expanduser(text)))


def use_hook_project_dir(data):
    project_dir = resolve_hook_project_dir(data)
    if not project_dir:
        return ""
    try:
        os.chdir(project_dir)
        log(f"Using hook project directory for Git snapshot: {project_dir}")
        return project_dir
    except Exception as exc:
        log(f"Failed to switch to hook project directory {project_dir}: {exc}", "WARN")
        return ""


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        log(f"State file invalid, resetting: {exc}", "WARN")
        return {}


def replace_file(source, target):
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except OSError:
            if attempt >= 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_text_atomic(path, content):
    tmp_path = f"{path}.tmp.{os.getpid()}.{int(time.time() * 1000000)}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    try:
        replace_file(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def save_state(path, data):
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_jsonl(path, data):
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        log(f"Failed to write log: {exc}", "WARN")


def write_decision_log(
    log_path,
    session_id,
    hook_event,
    agent_id,
    decision,
    reason,
    match="",
    message="",
    count=-1,
    continuation_prompt="",
    git_commit_hash="",
):
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": str(session_id),
        "hook_event": str(hook_event),
        "agent_id": str(agent_id),
        "decision": decision,
        "reason": reason,
        "match": str(match or ""),
        "count": count,
        "excerpt": normalize_text(message)[:500],
    }
    if continuation_prompt:
        entry["continuation_prompt"] = continuation_prompt
    if git_commit_hash:
        entry["git_commit_hash"] = str(git_commit_hash)
    write_jsonl(log_path, entry)


def training_continue_prompt(settings):
    custom = ""
    if isinstance(settings, dict):
        custom = str(settings.get("training_continue_prompt") or "").strip()
    if not custom:
        custom = DEFAULT_TRAINING_CONTINUE_PROMPT
    return (
        "请检查当前深度学习/模型训练任务的最新评估结果、训练日志、指标和模型产物。\n\n"
        "用户定义的训练目标/续跑要求：\n"
        f"{custom}\n\n"
        "如果尚未达标，请继续训练、调参、改进模型或补充验证，并记录新的评估结果。\n"
        "如果已达标，请停止续跑，并在最终回复中明确写出 TRAINING_TARGET_MET，"
        "同时列出关键指标和模型产物路径。"
    )


def permission_suggestions_from_input(data):
    raw = data.get("permission_suggestions")
    if raw is None:
        raw = data.get("permissionSuggestions")
    if raw is None:
        for key in ("permission_request", "permissionRequest", "request"):
            container = data.get(key)
            if not isinstance(container, dict):
                continue
            raw = container.get("permission_suggestions")
            if raw is None:
                raw = container.get("permissionSuggestions")
            if raw is not None:
                break
    if isinstance(raw, list):
        candidates = raw
    elif isinstance(raw, dict):
        candidates = [raw]
    else:
        candidates = []
    return [item for item in candidates if isinstance(item, dict)]


def permission_decision_updates(data, tool_name):
    updates = permission_suggestions_from_input(data)
    if not updates:
        updates = [
            {
                "type": "addRules",
                "rules": [{"toolName": tool_name}],
                "behavior": "allow",
                "destination": "session",
            }
        ]
    updates = list(updates)
    updates.append({"type": "setMode", "mode": "dontAsk", "destination": "session"})
    return updates


def ensure_gitignore():
    path = os.path.join(os.getcwd(), ".gitignore")
    if os.path.exists(path):
        return
    try:
        write_text_atomic(path, "\n".join(DEFAULT_GITIGNORE_LINES) + "\n")
        log("Created local .gitignore for Git snapshots")
    except Exception as exc:
        log(f"Failed to create local .gitignore: {exc}", "WARN")


def push_git_snapshot(auto_push=False):
    if not auto_push:
        return
    try:
        upstream = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if upstream.returncode == 0 and upstream.stdout.strip():
            push = subprocess.run(
                ["git", "push"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
        else:
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            remotes = subprocess.run(
                ["git", "remote"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            if branch.returncode != 0 or remotes.returncode != 0:
                log("Git auto push skipped: failed to inspect branch or remotes", "WARN")
                return
            remote_names = [line.strip() for line in remotes.stdout.splitlines() if line.strip()]
            branch_name = branch.stdout.strip()
            if not branch_name or not remote_names:
                log("Git auto push skipped: no upstream or remote", "WARN")
                return
            remote_name = "origin" if "origin" in remote_names else remote_names[0]
            push = subprocess.run(
                ["git", "push", "-u", remote_name, branch_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=60,
            )
        if push.returncode != 0:
            first_line = next((line.strip() for line in push.stdout.splitlines() if line.strip()), "unknown error")
            log(f"Git auto push failed: {first_line}", "WARN")
            return
        log("Git auto push completed")
    except Exception as exc:
        log(f"Git auto push failed: {exc}", "WARN")


def run_git_snapshot(auto_push=False):
    if not shutil.which("git"):
        return ""

    def run(args, timeout=15):
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)

    try:
        initialized_repo = False
        git_dir_result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if git_dir_result.returncode != 0:
            initialized_repo = run(["git", "init"]).returncode == 0
            if not initialized_repo:
                log("Git init did not complete; skipping git snapshot", "WARN")
                return ""
            git_dir_result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            if git_dir_result.returncode != 0 or not git_dir_result.stdout.strip():
                log("Git directory could not be resolved after init; skipping git snapshot", "WARN")
                return ""

        if initialized_repo:
            ensure_gitignore()

        git_dir = git_dir_result.stdout.strip()
        if git_dir and os.path.exists(os.path.join(git_dir, "index.lock")):
            log("Git index lock exists; skipping git snapshot", "WARN")
            return ""

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
        )
        if status.returncode != 0:
            first_line = next((line.strip() for line in status.stdout.splitlines() if line.strip()), "unknown error")
            log(f"Git status failed; skipping git snapshot: {first_line}", "WARN")
            return ""
        if not status.stdout.strip():
            return ""

        add_result = run(["git", "add", "-A"], timeout=30)
        if add_result.returncode != 0:
            log("Git add did not complete; skipping git snapshot", "WARN")
            return ""
        username = subprocess.run(
            ["git", "config", "user.name"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        email = subprocess.run(
            ["git", "config", "user.email"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if not username.stdout.strip() or not email.stdout.strip():
            run(["git", "config", "user.name", "API-Switcher-Auto"], timeout=5)
            run(["git", "config", "user.email", "auto@api-switcher.local"], timeout=5)

        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        commit = run(["git", "commit", "--no-verify", "-m", f"[git-snapshot] {stamp}"], timeout=30)
        if commit.returncode != 0:
            log("Git snapshot commit did not complete", "WARN")
            return ""
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        commit_hash = rev.stdout.strip() if rev.returncode == 0 else ""
        if commit_hash:
            push_git_snapshot(auto_push)
        return commit_hash
    except Exception as exc:
        log(f"Git snapshot failed: {exc}", "WARN")
        return ""


def main():
    settings_path = sys.argv[1]
    state_dir = sys.argv[2]
    input_path = sys.argv[3] if len(sys.argv) > 3 else ""
    try:
        with open(settings_path, "r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except FileNotFoundError:
        return
    except Exception as exc:
        log(f"Failed to load settings: {exc}", "ERROR")
        return

    auto_approve_enabled = as_bool(settings.get("auto_approve_permission_requests"), False)
    error_recovery_enabled = as_bool(settings.get("error_recovery_enabled"), False)
    auto_continue_enabled = as_bool(settings.get("enabled"), False)
    training_auto_continue_enabled = as_bool(settings.get("training_auto_continue_enabled"), False)
    git_snapshot_enabled = as_bool(settings.get("git_auto_snapshot"), True) and as_bool(
        settings.get("git_snapshot_on_start"),
        True,
    )

    if (
        not auto_continue_enabled
        and not training_auto_continue_enabled
        and not auto_approve_enabled
        and not git_snapshot_enabled
        and not error_recovery_enabled
    ):
        return

    raw_input = ""
    if input_path:
        try:
            with open(input_path, "r", encoding="utf-8", errors="replace") as handle:
                raw_input = handle.read()
        except Exception as exc:
            log(f"Failed to read hook input file: {exc}", "ERROR")
            return
    if not raw_input.strip():
        return
    try:
        data = json.loads(raw_input)
    except Exception as exc:
        log(f"Failed to parse hook input: {exc}", "ERROR")
        return
    if not isinstance(data, dict):
        return
    use_hook_project_dir(data)

    is_claude = data.get("hook_event_name") is not None or data.get("hookEventName") is not None
    hook_event = data.get("hook_event_name") or data.get("hookEventName") or "Stop"
    agent_id = data.get("agent_id") or data.get("agentId") or ""
    session_id = (
        data.get("session_id")
        or data.get("sessionId")
        or data.get("conversation_id")
        or data.get("transcript_path")
        or data.get("transcriptPath")
        or os.getcwd()
    )

    explicit_error_event = hook_event in {"ResponseError", "Error"}
    has_error_payload = any(
        key in data
        for key in (
            "error_message",
            "error_code",
            "error",
            "errorMessage",
            "hint",
            "detail",
            "response",
            "body",
            "errors",
            "stderr",
            "stdout",
        )
    )
    if error_recovery_enabled and (explicit_error_event or (not is_claude and has_error_payload)):
        if handle_error_recovery(data, settings, state_dir, is_claude, session_id):
            return

    git_snapshot_attempted = False
    git_snapshot_hash = ""
    if hook_event in PROMPT_SNAPSHOT_EVENTS:
        if git_snapshot_enabled:
            git_snapshot_hash = run_git_snapshot(as_bool(settings.get("git_auto_push"), False))
            git_snapshot_attempted = True
        return

    # Stop hooks are the broadest safety net: they cover normal manual turns,
    # auto-continue turns, and older Codex builds that do not emit prompt hooks.
    if hook_event in STOP_SNAPSHOT_EVENTS and git_snapshot_enabled:
        git_snapshot_hash = run_git_snapshot(as_bool(settings.get("git_auto_push"), False))
        git_snapshot_attempted = True

    if is_claude and hook_event in {"PermissionRequest", "PreToolUse"}:
        if not auto_approve_enabled:
            return
        permission_request = data.get("permission_request") if isinstance(data.get("permission_request"), dict) else {}
        permission_request_camel = data.get("permissionRequest") if isinstance(data.get("permissionRequest"), dict) else {}
        request = data.get("request") if isinstance(data.get("request"), dict) else {}
        tool_name = str(
            data.get("tool_name")
            or data.get("toolName")
            or data.get("tool")
            or permission_request.get("tool_name")
            or permission_request.get("toolName")
            or permission_request_camel.get("tool_name")
            or permission_request_camel.get("toolName")
            or request.get("tool_name")
            or request.get("toolName")
            or ""
        ).strip()
        if not tool_allowed(tool_name, permission_tools(settings)):
            return

        os.makedirs(state_dir, exist_ok=True)
        log_path = os.path.join(state_dir, "auto_continue_stop_log.jsonl")
        max_auto_approvals = as_int(settings.get("auto_approve_max_per_session"), 0)
        count = 0
        if max_auto_approvals > 0:
            state_seed = f"{session_id}|PermissionRequest|{agent_id}"
            state_key = hashlib.sha256(state_seed.encode("utf-8", errors="replace")).hexdigest()
            state_path = os.path.join(state_dir, "auto_continue_permission_state.json")
            lock_path = state_path + ".lock"

            lock_fd = None
            for _ in range(5):
                try:
                    lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    break
                except FileExistsError:
                    try:
                        if time.time() - os.path.getmtime(lock_path) > 15:
                            os.unlink(lock_path)
                            continue
                    except FileNotFoundError:
                        continue
                    except Exception:
                        pass
                    time.sleep(0.05)
            if lock_fd is None:
                return

            try:
                state = load_state(state_path)
                count = as_int(state.get(state_key), 0)
                if count >= max_auto_approvals:
                    write_jsonl(log_path, {
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "session_id": str(session_id),
                        "hook_event": "PermissionRequest",
                        "agent_id": str(agent_id),
                        "tool_name": tool_name,
                        "decision": "ask_user",
                        "reason": "auto_approve_limit_reached",
                        "count": count,
                    })
                    return

                count += 1
                state[state_key] = count
                save_state(state_path, state)
            finally:
                try:
                    if lock_fd is not None:
                        os.close(lock_fd)
                finally:
                    try:
                        os.unlink(lock_path)
                    except FileNotFoundError:
                        pass
                    except Exception:
                        pass

        write_jsonl(log_path, {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": str(session_id),
            "hook_event": "PermissionRequest",
            "agent_id": str(agent_id),
            "tool_name": tool_name,
            "decision": "auto_approve",
            "reason": "configured_permission_request",
            "count": count,
        })
        if hook_event == "PreToolUse":
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": "Auto-approved by API Switcher",
                }
            }
        else:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "allow",
                        "updatedPermissions": permission_decision_updates(data, tool_name),
                    },
                }
            }
        print(json.dumps(output, ensure_ascii=False))
        return

    if not auto_continue_enabled and not training_auto_continue_enabled:
        return

    last_message = pick_text(
        data,
        [
            "last_assistant_message",
            "lastAssistantMessage",
            "last_message",
            "lastMessage",
            "assistant_message",
            "assistantMessage",
            "message",
            "content",
            "text",
        ],
    )
    if not last_message:
        transcript_path = data.get("transcript_path") or data.get("transcriptPath")
        last_message = transcript_tail(transcript_path)
    last_message = normalize_text(last_message)
    if not last_message.strip():
        return

    max_continuations = as_int(settings.get("max_continuations"), 100)
    if max_continuations == 0 or max_continuations < -1:
        return

    if is_claude and as_bool(settings.get("conservative_mode"), True) and as_bool(data.get("stop_hook_active"), False):
        return

    state_seed = f"{session_id}|{hook_event}|{agent_id}"
    state_key = hashlib.sha256(state_seed.encode("utf-8", errors="replace")).hexdigest()

    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "auto_continue_stop_state.json")
    log_path = os.path.join(state_dir, "auto_continue_stop_log.jsonl")
    lock_path = state_path + ".lock"

    recoverable_api_error_match = matching_pattern(RECOVERABLE_API_ERROR_PATTERNS, last_message)
    recoverable_api_error = bool(recoverable_api_error_match)

    training_guard_applies = False
    training_context_match = ""
    if training_auto_continue_enabled:
        training_target_met_match = matching_pattern(TRAINING_COMPLETION_PATTERNS, last_message)
        if training_target_met_match:
            state = load_state(state_path)
            if state_key in state:
                state.pop(state_key, None)
                save_state(state_path, state)
            write_decision_log(
                log_path,
                session_id,
                hook_event,
                agent_id,
                "allow_stop",
                "training_target_met",
                training_target_met_match,
                last_message,
                git_commit_hash=git_snapshot_hash,
            )
            return

        training_skip_match = matching_pattern(TRAINING_SKIP_PATTERNS, last_message)
        if training_skip_match:
            state = load_state(state_path)
            if state_key in state:
                state.pop(state_key, None)
                save_state(state_path, state)
            write_decision_log(
                log_path,
                session_id,
                hook_event,
                agent_id,
                "allow_stop",
                "training_not_applicable",
                training_skip_match,
                last_message,
                git_commit_hash=git_snapshot_hash,
            )
            return

        training_context_match = matching_pattern(TRAINING_CONTEXT_PATTERNS, last_message)
        training_guard_applies = bool(training_context_match)

    blocker_match = matching_pattern(settings.get("blocker_patterns"), last_message)
    if blocker_match and not recoverable_api_error:
        write_decision_log(
            log_path,
            session_id,
            hook_event,
            agent_id,
            "allow_stop",
            "blocker_detected",
            blocker_match,
            last_message,
            git_commit_hash=git_snapshot_hash,
        )
        return

    incomplete_match = matching_pattern(settings.get("incomplete_patterns"), last_message)
    generic_continue_match = bool(auto_continue_enabled and incomplete_match)
    should_continue = recoverable_api_error or training_guard_applies or generic_continue_match
    if not should_continue:
        state = load_state(state_path)
        if state_key in state:
            state.pop(state_key, None)
            save_state(state_path, state)
        allow_reason = (
            "training_context_not_detected"
            if training_auto_continue_enabled and not auto_continue_enabled
            else "no_incomplete_match"
        )
        write_decision_log(
            log_path,
            session_id,
            hook_event,
            agent_id,
            "allow_stop",
            allow_reason,
            "",
            last_message,
            git_commit_hash=git_snapshot_hash,
        )
        return

    matched_pattern = (
        recoverable_api_error_match
        if recoverable_api_error
        else training_context_match
        if training_guard_applies
        else incomplete_match
    )

    lock_fd = None
    for _ in range(20):
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 60:
                    os.unlink(lock_path)
                    continue
            except FileNotFoundError:
                continue
            except Exception:
                pass
            time.sleep(0.1)
    if lock_fd is None:
        log("Failed to acquire state lock", "WARN")
        return

    try:
        state = load_state(state_path)
        count = as_int(state.get(state_key), 0)
        if max_continuations >= 0 and count >= max_continuations:
            write_decision_log(
                log_path,
                session_id,
                hook_event,
                agent_id,
                "allow_stop",
                "max_continuations_reached",
                matched_pattern,
                last_message,
                count,
                git_commit_hash=git_snapshot_hash,
            )
            return

        count += 1
        state[state_key] = count
        save_state(state_path, state)

        if training_guard_applies and not recoverable_api_error:
            continuation_prompt = training_continue_prompt(settings)
        else:
            continuation_prompt = settings.get("continuation_prompt") or "Please continue from where you left off. Complete any remaining work."
        continue_reason = (
            "recoverable_api_error_detected"
            if recoverable_api_error
            else "training_guard_continue"
            if training_guard_applies
            else "incomplete_work_detected"
        )

        if hook_event in STOP_SNAPSHOT_EVENTS and git_snapshot_enabled and not git_snapshot_attempted:
            git_snapshot_hash = run_git_snapshot(as_bool(settings.get("git_auto_push"), False))

        write_decision_log(
            log_path,
            session_id,
            hook_event,
            agent_id,
            "block_stop",
            continue_reason,
            matched_pattern,
            last_message,
            count,
            continuation_prompt,
            git_commit_hash=git_snapshot_hash,
        )

        output = {
            "decision": "block",
            "reason": continuation_prompt,
            "suppressOutput": True,
        }
        print(json.dumps(output, ensure_ascii=False))
    finally:
        try:
            if lock_fd is not None:
                os.close(lock_fd)
        finally:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass
            except Exception:
                pass


try:
    main()
except Exception as exc:
    log(f"Unexpected hook error: {exc}", "ERROR")
PY
else
  echo "Python 3.6+ not found; auto-continue hook skipped" >&2
fi
rm -f "$INPUT_PATH" 2>/dev/null || true
exit 0
'''
    body = body.replace(
        "__RECOVERABLE_API_ERROR_PATTERNS__",
        _python_literal_list(RECOVERABLE_API_ERROR_PATTERNS),
    )
    body = body.replace(
        "__CONTENT_LENGTH_PATTERNS__",
        _python_literal_list(CONTENT_LENGTH_PATTERNS),
    )
    body = body.replace(
        "__TRAINING_COMPLETION_PATTERNS__",
        _python_literal_list(DEFAULT_TRAINING_COMPLETION_PATTERNS),
    )
    body = body.replace(
        "__TRAINING_SKIP_PATTERNS__",
        _python_literal_list(DEFAULT_TRAINING_SKIP_PATTERNS),
    )
    body = body.replace(
        "__TRAINING_CONTEXT_PATTERNS__",
        _python_literal_list(DEFAULT_TRAINING_CONTEXT_PATTERNS),
    )
    body = body.replace(
        "__DEFAULT_TRAINING_CONTINUE_PROMPT__",
        repr(DEFAULT_TRAINING_CONTINUE_PROMPT),
    )
    return header + body


def _is_our_command(command: str) -> bool:
    return any(marker in str(command or "") for marker in SCRIPT_MARKERS)


def _claude_event_has_our_command(settings: dict, event_name: str) -> bool:
    return any(_is_our_command(command) for command in _iter_claude_hook_commands(settings, (event_name,)))


def _iter_claude_hook_commands(
    settings: dict,
    event_names: tuple[str, ...] = (
        "Stop",
        "SubagentStop",
        "UserPromptSubmit",
        "SessionStart",
        "PreToolUse",
        "PermissionRequest",
        "ResponseError",
    ),
):
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return
    for event_name in event_names:
        groups = hooks.get(event_name, [])
        if isinstance(groups, dict):
            groups = [groups]
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            hook_list = group.get("hooks", [])
            if isinstance(hook_list, dict):
                hook_list = [hook_list]
            if not isinstance(hook_list, list):
                continue
            for hook in hook_list:
                if isinstance(hook, dict):
                    yield str(hook.get("command", ""))


def _read_managed_permission_state(client, path: str) -> tuple[list[str], list[str]]:
    payload = _read_json(client, path, default={}, strict=False)
    return rules_from_payload(payload), ask_rules_from_payload(payload)


def _write_managed_permission_state(client, path: str, rules: list[str], ask_rules: list[str]) -> None:
    if rules or ask_rules:
        _write_json(client, path, rules_payload(rules, ask_rules), mode=0o600)
    else:
        _remove_remote_file(client, path)


def _register_claude_hook(
    client,
    paths: RemoteAutoContinuePaths,
    command: str,
    apply_to_subagents: bool,
    settings_data: AutoContinueSettings | None = None,
) -> None:
    settings = _read_json_object_for_update(client, paths.provider_config_path, "Claude settings.json")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks

    hook_def = {
        "type": "command",
        "command": command,
        "timeout": 10,
        "statusMessage": "Checking whether Claude should continue",
    }

    def register_event(event_name: str, hook: dict | None) -> None:
        groups = hooks.get(event_name, [])
        if isinstance(groups, dict):
            groups = [groups]
        if not isinstance(groups, list):
            groups = []
        filtered = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            hook_list = group.get("hooks", [])
            if isinstance(hook_list, dict):
                hook_list = [hook_list]
            if not isinstance(hook_list, list):
                hook_list = []
            remaining = [h for h in hook_list if not _is_our_command(h.get("command", "") if isinstance(h, dict) else "")]
            if remaining:
                new_group = dict(group)
                new_group["hooks"] = remaining
                filtered.append(new_group)
        if hook:
            filtered.append({"hooks": [hook]})
        hooks[event_name] = filtered

    needs_git_start_hook = (
        True
        if settings_data is None
        else bool(settings_data.git_auto_snapshot and settings_data.git_snapshot_on_start)
    )
    needs_stop_hook = (
        True
        if settings_data is None
        else bool(settings_data.enabled or settings_data.training_auto_continue_enabled or needs_git_start_hook)
    )
    register_event("Stop", hook_def if needs_stop_hook else None)
    if needs_git_start_hook:
        prompt_hook = dict(hook_def)
        prompt_hook["statusMessage"] = "Creating Git snapshot before Claude starts work"
        register_event("UserPromptSubmit", prompt_hook)
        session_hook = dict(hook_def)
        session_hook["statusMessage"] = "Creating Git snapshot when Claude session starts"
        register_event("SessionStart", session_hook)
    else:
        register_event("UserPromptSubmit", None)
        register_event("SessionStart", None)
    if apply_to_subagents and needs_stop_hook:
        subagent_hook = dict(hook_def)
        subagent_hook["statusMessage"] = "Checking whether Claude subagent should continue"
        register_event("SubagentStop", subagent_hook)
    else:
        register_event("SubagentStop", None)
    if settings_data and settings_data.auto_approve_permission_requests:
        permissions = settings.get("permissions")
        permissions = dict(permissions) if isinstance(permissions, dict) else {}
        permissions["defaultMode"] = "dontAsk"
        settings["permissions"] = permissions
        settings["skipDangerousModePermissionPrompt"] = False

        pre_tool_hook = dict(hook_def)
        pre_tool_hook["statusMessage"] = "Auto-allowing configured Claude tool call if allowed"
        register_event("PreToolUse", pre_tool_hook)
        permission_hook = dict(hook_def)
        permission_hook["statusMessage"] = "Auto-approving configured Claude permission request if allowed"
        register_event("PermissionRequest", permission_hook)
    else:
        register_event("PreToolUse", None)
        register_event("PermissionRequest", None)
    if settings_data and settings_data.error_recovery_enabled:
        error_hook = dict(hook_def)
        error_hook["statusMessage"] = "Checking for API errors and auto-recovery"
        register_event("ResponseError", error_hook)
    else:
        register_event("ResponseError", None)

    previous_rules, previous_ask_rules = _read_managed_permission_state(client, paths.permission_rules_path)
    desired_rules = permission_rules_from_auto_settings(settings_data)
    settings, managed_rules, removed_ask_rules = apply_managed_permission_rules(
        settings,
        desired_rules,
        previous_rules,
        previous_ask_rules,
    )

    _write_json(client, paths.provider_config_path, settings)
    _write_managed_permission_state(client, paths.permission_rules_path, managed_rules, removed_ask_rules)


def _unregister_claude_hook(client, paths: RemoteAutoContinuePaths) -> None:
    settings = _read_json(client, paths.provider_config_path, default={}, strict=False)
    if not isinstance(settings, dict):
        _write_managed_permission_state(client, paths.permission_rules_path, [], [])
        return
    hooks = settings.get("hooks", {})

    changed = False
    if isinstance(hooks, dict):
        for event_name in (
            "Stop",
            "SubagentStop",
            "UserPromptSubmit",
            "SessionStart",
            "PreToolUse",
            "PermissionRequest",
            "ResponseError",
        ):
            groups = hooks.get(event_name, [])
            if isinstance(groups, dict):
                groups = [groups]
            if not isinstance(groups, list):
                continue
            filtered_groups = []
            for group in groups:
                if not isinstance(group, dict):
                    continue
                hook_list = group.get("hooks", [])
                if isinstance(hook_list, dict):
                    hook_list = [hook_list]
                if not isinstance(hook_list, list):
                    hook_list = []
                remaining = [
                    h for h in hook_list
                    if not _is_our_command(h.get("command", "") if isinstance(h, dict) else "")
                ]
                if len(remaining) != len(hook_list):
                    changed = True
                if remaining:
                    new_group = dict(group)
                    new_group["hooks"] = remaining
                    filtered_groups.append(new_group)
            hooks[event_name] = filtered_groups

    previous_rules, previous_ask_rules = _read_managed_permission_state(client, paths.permission_rules_path)
    if previous_rules or previous_ask_rules:
        settings, _managed_rules, _removed_ask_rules = apply_managed_permission_rules(
            settings,
            [],
            previous_rules,
            previous_ask_rules,
        )
        _write_managed_permission_state(client, paths.permission_rules_path, [], [])
        changed = True

    if changed:
        _write_json(client, paths.provider_config_path, settings)


def _iter_codex_hook_commands(data: dict, event_name: str):
    if not isinstance(data, dict):
        return
    hooks = _codex_hooks_container(data)
    for value in (hooks.get(event_name), data.get(event_name)):
        for hook in _codex_event_hooks(value):
            command = hook.get("command")
            if command:
                yield str(command)


def _codex_event_hooks(value) -> list[dict]:
    if isinstance(value, dict):
        hooks = []
        if value.get("command"):
            hooks.append(dict(value))
        nested = value.get("hooks")
        if isinstance(nested, list):
            hooks.extend(dict(hook) for hook in nested if isinstance(hook, dict) and hook.get("command"))
        return hooks
    if isinstance(value, list):
        hooks = []
        for item in value:
            if not isinstance(item, dict):
                continue
            if item.get("command"):
                hooks.append(dict(item))
            nested = item.get("hooks")
            if isinstance(nested, list):
                hooks.extend(dict(hook) for hook in nested if isinstance(hook, dict) and hook.get("command"))
        return hooks
    return []


def _codex_hooks_container(data: dict, *, migrate_legacy: bool = False) -> dict:
    if not isinstance(data, dict):
        return {}

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks

    if migrate_legacy:
        for event_name in list(data.keys()):
            if event_name == "hooks":
                continue
            event_hooks = _codex_event_hooks(data.get(event_name))
            managed_hooks = [
                hook for hook in event_hooks
                if _is_our_command(str(hook.get("command", "")))
            ]
            if not managed_hooks:
                continue
            existing = _codex_event_hooks(hooks.get(event_name))
            hooks[event_name] = _format_codex_event_hooks(existing + managed_hooks)

            remaining = [
                hook for hook in event_hooks
                if not _is_our_command(str(hook.get("command", "")))
            ]
            formatted = _format_legacy_codex_event_hooks(remaining)
            if formatted is None:
                data.pop(event_name, None)
            else:
                data[event_name] = formatted

    return hooks


def _format_codex_event_hooks(hook_list: list[dict]):
    if not hook_list:
        return None
    return [{"hooks": hook_list}]


def _format_legacy_codex_event_hooks(hook_list: list[dict]):
    if not hook_list:
        return None
    if len(hook_list) == 1:
        return hook_list[0]
    return {"hooks": hook_list}


def _upsert_codex_event_hook(hooks: dict, event_name: str, hook_def: dict) -> None:
    existing = [
        hook for hook in _codex_event_hooks(hooks.get(event_name))
        if not _is_our_command(str(hook.get("command", "")))
    ]
    existing.append(hook_def)
    hooks[event_name] = _format_codex_event_hooks(existing)


def _remove_codex_event_hook(hooks: dict, event_name: str) -> bool:
    if event_name not in hooks:
        return False
    existing = _codex_event_hooks(hooks.get(event_name))
    remaining = [
        hook for hook in existing
        if not _is_our_command(str(hook.get("command", "")))
    ]
    if len(remaining) == len(existing):
        return False
    formatted = _format_codex_event_hooks(remaining)
    if formatted is None:
        hooks.pop(event_name, None)
    else:
        hooks[event_name] = formatted
    return True


def _codex_hooks_has_entries(hooks: dict) -> bool:
    if not isinstance(hooks, dict):
        return False
    return any(_codex_event_hooks(value) for value in hooks.values())


def _codex_data_has_entries(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    hooks = _codex_hooks_container(data)
    if _codex_hooks_has_entries(hooks):
        return True
    return any(
        _codex_event_hooks(value)
        for key, value in data.items()
        if key != "hooks"
    )


def _codex_hooks_enabled_from_config(config: dict) -> bool:
    if not isinstance(config, dict):
        return False
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    if "codex_hooks" in features:
        return bool(features.get("codex_hooks"))
    return bool(config.get("codex_hooks"))


def _set_codex_hooks_enabled(client, paths: RemoteAutoContinuePaths, enabled: bool) -> None:
    config = _read_toml(client, paths.provider_config_path, strict=False)
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    if enabled or "codex_hooks" in features:
        features["codex_hooks"] = bool(enabled)
        config["features"] = features
    if config.get("codex_hooks") is not None:
        config["codex_hooks"] = bool(enabled)
    _write_toml(client, paths.provider_config_path, config)


def _register_codex_hook(
    client,
    paths: RemoteAutoContinuePaths,
    command: str,
    settings_data: AutoContinueSettings | None = None,
) -> None:
    if not paths.codex_hooks_path:
        raise RuntimeError("Codex hooks 路径缺失")
    data = _read_codex_hooks_json_for_update(client, paths.codex_hooks_path)
    hooks = _codex_hooks_container(data, migrate_legacy=True)
    needs_git_start_hook = (
        True
        if settings_data is None
        else bool(settings_data.git_auto_snapshot and settings_data.git_snapshot_on_start)
    )
    needs_stop_hook = (
        True
        if settings_data is None
        else bool(settings_data.enabled or settings_data.training_auto_continue_enabled or needs_git_start_hook)
    )
    hook_def = {
        "type": "command",
        "command": command,
        "timeout": 10,
        "statusMessage": "Checking whether Codex should continue",
    }
    if needs_stop_hook:
        _upsert_codex_event_hook(
            hooks,
            "Stop",
            hook_def,
        )
    else:
        _remove_codex_event_hook(hooks, "Stop")
    if needs_git_start_hook:
        prompt_hook = dict(hook_def)
        prompt_hook["statusMessage"] = "Creating Git snapshot before Codex starts work"
        _upsert_codex_event_hook(hooks, "UserPromptSubmit", prompt_hook)
        session_hook = dict(hook_def)
        session_hook["statusMessage"] = "Creating Git snapshot when Codex session starts"
        _upsert_codex_event_hook(hooks, "SessionStart", session_hook)
    else:
        _remove_codex_event_hook(hooks, "UserPromptSubmit")
        _remove_codex_event_hook(hooks, "SessionStart")
    if settings_data and settings_data.error_recovery_enabled:
        _upsert_codex_event_hook(
            hooks,
            "Error",
            {
                "type": "command",
                "command": command,
                "timeout": 10,
                "statusMessage": "Checking for Codex API errors and auto-recovery",
            },
        )
    else:
        _remove_codex_event_hook(hooks, "Error")
    _write_json(client, paths.codex_hooks_path, data)

    _set_codex_hooks_enabled(client, paths, True)


def _unregister_codex_hook(client, paths: RemoteAutoContinuePaths) -> None:
    if not paths.codex_hooks_path:
        return
    data = _read_json(client, paths.codex_hooks_path, default={}, strict=False)
    if isinstance(data, dict):
        hooks = _codex_hooks_container(data, migrate_legacy=True)
        changed = _remove_codex_event_hook(hooks, "Stop")
        changed = _remove_codex_event_hook(hooks, "UserPromptSubmit") or changed
        changed = _remove_codex_event_hook(hooks, "SessionStart") or changed
        changed = _remove_codex_event_hook(hooks, "Error") or changed
        if changed:
            _write_json(client, paths.codex_hooks_path, data)
            if not _codex_data_has_entries(data):
                _set_codex_hooks_enabled(client, paths, False)


def _install_guidance(client, path: str) -> None:
    existing = _read_text(client, path) or ""
    import re

    pattern = r"<!-- BEGIN AUTO CONTINUE GUIDANCE -->.*?<!-- END AUTO CONTINUE GUIDANCE -->"
    if "BEGIN AUTO CONTINUE GUIDANCE" in existing:
        content = re.sub(pattern, AUTO_CONTINUE_GUIDANCE.strip(), existing, flags=re.DOTALL)
    else:
        content = existing
        if content and not content.endswith("\n"):
            content += "\n\n"
        content += AUTO_CONTINUE_GUIDANCE
    _write_text(client, path, content, mode=0o600)


def _uninstall_guidance(client, path: str) -> None:
    existing = _read_text(client, path)
    if not existing or "BEGIN AUTO CONTINUE GUIDANCE" not in existing:
        return
    import re

    pattern = r"<!-- BEGIN AUTO CONTINUE GUIDANCE -->.*?<!-- END AUTO CONTINUE GUIDANCE -->\n*"
    content = re.sub(pattern, "", existing, flags=re.DOTALL)
    if content.strip():
        _write_text(client, path, content, mode=0o600)
    else:
        _remove_remote_file(client, path)


def install_remote_auto_continue(
    ssh_name: str,
    provider_name: str,
    settings: AutoContinueSettings | None = None,
) -> str:
    """Install or repair remote auto-continue for one provider."""
    provider = _normal_provider(provider_name)
    resolved_settings = _load_local_settings(provider, settings)
    ssh_profile, client = _connect(ssh_name)
    _ensure_remote_runtime(
        client,
        ssh_profile,
        require_git=bool(
            resolved_settings.git_auto_snapshot
            and (
                resolved_settings.git_snapshot_on_start
                or (resolved_settings.error_recovery_enabled and resolved_settings.git_snapshot_on_recovery)
            )
        ),
    )

    paths = _paths(client, ssh_profile, provider)
    snapshot_paths = [
        paths.settings_path,
        paths.script_path,
        paths.guidance_path,
        paths.provider_config_path,
        paths.permission_rules_path,
    ]
    if paths.codex_hooks_path:
        snapshot_paths.append(paths.codex_hooks_path)
    snapshots = _snapshot_remote_files(client, snapshot_paths)

    try:
        _write_json(client, paths.settings_path, resolved_settings.to_dict())

        script = _generate_remote_hook_script(paths.settings_path, paths.state_dir)
        _write_text(client, paths.script_path, script, mode=0o700)
        _install_guidance(client, paths.guidance_path)

        command = f"sh {shlex.quote(paths.script_path)}"
        if provider == "claude":
            _register_claude_hook(client, paths, command, resolved_settings.apply_to_subagents, resolved_settings)
        else:
            _register_codex_hook(client, paths, command, resolved_settings)
    except Exception:
        _restore_remote_files(client, snapshots)
        raise

    logger.info(f"Installed remote auto-continue for {provider} on {ssh_profile.host}")
    return f"已在 {ssh_profile.host} 安装/修复 {_provider_label(provider)} 远端自动续跑"


def install_remote_git_snapshot(
    ssh_name: str,
    provider_name: str,
    settings: AutoContinueSettings | None = None,
) -> str:
    """Install only the remote Git snapshot stop hook for one provider."""
    provider = _normal_provider(provider_name)
    resolved_settings = _load_git_snapshot_settings(provider, settings)
    ssh_profile, client = _connect(ssh_name)
    _ensure_remote_runtime(client, ssh_profile, require_git=True)

    paths = _paths(client, ssh_profile, provider)
    snapshot_paths = [
        paths.settings_path,
        paths.script_path,
        paths.provider_config_path,
        paths.permission_rules_path,
    ]
    if paths.codex_hooks_path:
        snapshot_paths.append(paths.codex_hooks_path)
    snapshots = _snapshot_remote_files(client, snapshot_paths)

    try:
        _write_json(client, paths.settings_path, resolved_settings.to_dict())

        script = _generate_remote_hook_script(paths.settings_path, paths.state_dir)
        _write_text(client, paths.script_path, script, mode=0o700)

        command = f"sh {shlex.quote(paths.script_path)}"
        if provider == "claude":
            _register_claude_hook(client, paths, command, resolved_settings.apply_to_subagents, resolved_settings)
        else:
            _register_codex_hook(client, paths, command, resolved_settings)
    except Exception:
        _restore_remote_files(client, snapshots)
        raise

    logger.info(f"Installed remote Git snapshot hook for {provider} on {ssh_profile.host}")
    return f"已在 {ssh_profile.host} 安装 {_provider_label(provider)} 远端 Git 快照 Hook"


def update_remote_auto_continue_settings(
    ssh_name: str,
    provider_name: str,
    updates: dict[str, Any],
) -> str:
    """Update selected remote auto-continue settings and reconcile hooks."""
    provider = _normal_provider(provider_name)
    if not isinstance(updates, dict) or not updates:
        raise ValueError("No remote auto-continue settings to update")

    ssh_profile, client = _connect(ssh_name)
    paths = _paths(client, ssh_profile, provider)
    resolved_settings = _load_remote_settings_for_update(client, paths, provider)

    for key, value in updates.items():
        if key not in AutoContinueSettings.__dataclass_fields__:
            raise ValueError(f"Unsupported auto-continue setting: {key}")
        setattr(resolved_settings, key, value)

    if provider == "codex":
        resolved_settings.apply_to_subagents = False
        resolved_settings.auto_approve_permission_requests = False

    valid, error = resolved_settings.validate()
    if not valid:
        raise ValueError(f"Remote auto-continue settings invalid: {error}")

    hook_required = _settings_require_remote_hook(provider, resolved_settings)
    git_required = _settings_require_remote_git(resolved_settings)
    if hook_required:
        _ensure_remote_runtime(client, ssh_profile, require_git=git_required)

    snapshot_paths = [
        paths.settings_path,
        paths.script_path,
        paths.guidance_path,
        paths.provider_config_path,
        paths.permission_rules_path,
    ]
    if paths.codex_hooks_path:
        snapshot_paths.append(paths.codex_hooks_path)
    snapshots = _snapshot_remote_files(client, snapshot_paths)

    try:
        _write_json(client, paths.settings_path, resolved_settings.to_dict())

        if hook_required:
            script = _generate_remote_hook_script(paths.settings_path, paths.state_dir)
            _write_text(client, paths.script_path, script, mode=0o700)
            if resolved_settings.enabled:
                _install_guidance(client, paths.guidance_path)
            else:
                _uninstall_guidance(client, paths.guidance_path)

            command = f"sh {shlex.quote(paths.script_path)}"
            if provider == "claude":
                _register_claude_hook(
                    client,
                    paths,
                    command,
                    resolved_settings.apply_to_subagents,
                    resolved_settings,
                )
            else:
                _register_codex_hook(client, paths, command, resolved_settings)
        else:
            if provider == "claude":
                _unregister_claude_hook(client, paths)
            else:
                _unregister_codex_hook(client, paths)
            _uninstall_guidance(client, paths.guidance_path)
    except Exception:
        _restore_remote_files(client, snapshots)
        raise

    logger.info(f"Updated remote auto-continue settings for {provider} on {ssh_profile.host}: {updates}")
    return f"\u5df2\u66f4\u65b0 {ssh_profile.host} \u7684 {_provider_label(provider)} \u8fdc\u7a0b\u81ea\u52a8\u7eed\u8dd1\u8bbe\u7f6e"


def pause_remote_auto_continue(ssh_name: str, provider_name: str) -> str:
    """Disable remote auto-continue while keeping script/settings on the server."""
    provider = _normal_provider(provider_name)
    ssh_profile, client = _connect(ssh_name)
    paths = _paths(client, ssh_profile, provider)

    settings = _read_json(client, paths.settings_path, default={}, strict=False)
    keep_hook = False
    if isinstance(settings, dict):
        settings["enabled"] = False
        keep_hook = (
            _as_bool_value(settings.get("git_auto_snapshot"), True)
            and _as_bool_value(settings.get("git_snapshot_on_start"), True)
        ) or _as_bool_value(settings.get("training_auto_continue_enabled"), False) or _as_bool_value(
            settings.get("error_recovery_enabled"), False
        ) or (
            provider == "claude"
            and _as_bool_value(settings.get("auto_approve_permission_requests"), False)
        )
        _write_json(client, paths.settings_path, settings)

    if not keep_hook:
        if provider == "claude":
            _unregister_claude_hook(client, paths)
        else:
            _unregister_codex_hook(client, paths)

    return f"已暂停 {ssh_profile.host} 的 {_provider_label(provider)} 远端自动续跑"


def uninstall_remote_auto_continue(ssh_name: str, provider_name: str) -> str:
    """Remove remote auto-continue hook, script, settings and guidance."""
    provider = _normal_provider(provider_name)
    ssh_profile, client = _connect(ssh_name)
    paths = _paths(client, ssh_profile, provider)

    if provider == "claude":
        _unregister_claude_hook(client, paths)
    else:
        _unregister_codex_hook(client, paths)

    for path in [
        paths.script_path,
        paths.settings_path,
        paths.permission_rules_path,
        posixpath.join(paths.state_dir, "auto_continue_stop_state.json"),
        posixpath.join(paths.state_dir, "auto_continue_stop_state.json.lock"),
        posixpath.join(paths.state_dir, "auto_continue_stop_log.jsonl"),
        posixpath.join(paths.state_dir, "error_recovery_state.json"),
        posixpath.join(paths.state_dir, "error_recovery_state.json.lock"),
        posixpath.join(paths.state_dir, "error_recovery_state.json.tmp"),
        posixpath.join(paths.state_dir, "error_recovery_log.jsonl"),
    ]:
        _remove_remote_file(client, path)
    _uninstall_guidance(client, paths.guidance_path)

    return f"已卸载 {ssh_profile.host} 的 {_provider_label(provider)} 远端自动续跑"


def get_remote_auto_continue_status(ssh_name: str, provider_name: str) -> RemoteAutoContinueStatus:
    """Inspect remote auto-continue status for one provider."""
    provider = _normal_provider(provider_name)
    ssh_profile, client = _connect(ssh_name)
    env = _probe_remote_environment(client)
    paths = _paths(client, ssh_profile, provider)

    status = RemoteAutoContinueStatus(
        provider_name=provider,
        remote_os=env.get("os", "unknown"),
        config_dir=paths.config_dir,
        script_path=paths.script_path,
        settings_path=paths.settings_path,
        git_available=bool(env.get("git")),
        runtime_ready=bool(env.get("is_posix") and env.get("sh") and env.get("python")),
    )

    if not env.get("is_posix"):
        status.issues.append(f"远端系统暂不支持: {env.get('os') or 'unknown'}")
    if not env.get("sh"):
        status.issues.append("缺少 sh")
    if not env.get("python"):
        status.issues.append("缺少 Python 3.6+")

    status.hook_script_exists = _remote_file_exists(client, paths.script_path)
    if status.hook_script_exists:
        status.hook_script_mode = _remote_file_mode(client, paths.script_path)
        remote_script = _read_text(client, paths.script_path)
        if remote_script is not None:
            status.hook_script_sha256 = _sha256_text(remote_script)
            expected_script = _generate_remote_hook_script(paths.settings_path, paths.state_dir)
            status.expected_hook_script_sha256 = _sha256_text(expected_script)
            status.hook_script_matches_expected = (
                status.hook_script_sha256 == status.expected_hook_script_sha256
            )
        if status.hook_script_mode is not None and not (status.hook_script_mode & 0o111):
            status.issues.append("Hook 脚本缺少可执行权限")

    try:
        settings = _read_json(client, paths.settings_path, default=None, strict=False)
    except Exception as e:
        settings = None
        status.issues.append(f"设置读取失败: {e}")
    parsed_settings = None
    if isinstance(settings, dict):
        try:
            parsed = AutoContinueSettings.from_dict(settings)
            parsed_settings = parsed
            status.settings_valid = True
            canonical_settings = parsed.to_dict()
            status.settings_sha256 = _sha256_json(settings)
            status.expected_settings_sha256 = _sha256_json(canonical_settings)
            status.settings_matches_expected = status.settings_sha256 == status.expected_settings_sha256
            status.enabled = parsed.enabled
            status.git_snapshot_master_enabled = bool(parsed.git_auto_snapshot)
            status.git_snapshot_on_start_enabled = bool(parsed.git_snapshot_on_start)
            status.git_snapshot_on_recovery_enabled = bool(parsed.git_snapshot_on_recovery)
            status.git_auto_push_enabled = bool(parsed.git_auto_push)
            status.training_auto_continue_enabled = bool(parsed.training_auto_continue_enabled)
            status.git_snapshot_enabled = bool(parsed.git_auto_snapshot and parsed.git_snapshot_on_start)
            status.permission_auto_approve_enabled = bool(
                provider == "claude" and parsed.auto_approve_permission_requests
            )
            status.error_recovery_enabled = bool(parsed.error_recovery_enabled)
            recovery_git_snapshot_enabled = bool(
                parsed.error_recovery_enabled
                and parsed.git_auto_snapshot
                and parsed.git_snapshot_on_recovery
            )
            if (status.git_snapshot_enabled or recovery_git_snapshot_enabled) and not env.get("git"):
                status.issues.append("缺少 git")
            if not status.settings_matches_expected:
                status.issues.append("自动续跑设置不是最新格式；请一键修复")
        except Exception as e:
            status.issues.append(f"设置无效: {e}")
    else:
        status.issues.append("缺少自动续跑设置")

    try:
        guidance = _read_text(client, paths.guidance_path)
    except Exception as e:
        guidance = None
        status.issues.append(f"指导文件读取失败: {e}")
    status.guidance_installed = bool(guidance and "BEGIN AUTO CONTINUE GUIDANCE" in guidance)

    if provider == "claude":
        try:
            provider_config = _read_json(client, paths.provider_config_path, default={}, strict=True)
        except Exception as e:
            provider_config = None
            status.issues.append(f"Claude settings.json 读取失败: {e}")
        if isinstance(provider_config, dict):
            stop_hook_registered = _claude_event_has_our_command(provider_config, "Stop")
            prompt_hook_registered = _claude_event_has_our_command(provider_config, "UserPromptSubmit")
            session_hook_registered = _claude_event_has_our_command(provider_config, "SessionStart")
            pre_tool_hook_registered = _claude_event_has_our_command(provider_config, "PreToolUse")
            permission_hook_registered = _claude_event_has_our_command(provider_config, "PermissionRequest")
            error_hook_registered = _claude_event_has_our_command(provider_config, "ResponseError")
            needs_stop_hook = bool(status.enabled or status.training_auto_continue_enabled or status.git_snapshot_enabled)
            needs_prompt_snapshot_hooks = bool(status.git_snapshot_enabled)
            needs_permission_hooks = bool(status.permission_auto_approve_enabled)
            needs_error_hook = bool(status.error_recovery_enabled)
            if needs_stop_hook or needs_prompt_snapshot_hooks or needs_permission_hooks or needs_error_hook:
                status.hook_registered = (
                    (not needs_stop_hook or stop_hook_registered)
                    and (
                        not needs_prompt_snapshot_hooks
                        or (prompt_hook_registered and session_hook_registered)
                    )
                    and (not needs_permission_hooks or (pre_tool_hook_registered and permission_hook_registered))
                    and (not needs_error_hook or error_hook_registered)
                )
            else:
                status.hook_registered = any(
                    _is_our_command(command)
                    for command in _iter_claude_hook_commands(provider_config)
                )
            if needs_stop_hook and not stop_hook_registered:
                status.issues.append("Stop Hook 未注册；请重新安装/修复远端自动续跑")
            if needs_prompt_snapshot_hooks:
                missing_prompt_hooks = []
                if not prompt_hook_registered:
                    missing_prompt_hooks.append("UserPromptSubmit")
                if not session_hook_registered:
                    missing_prompt_hooks.append("SessionStart")
                if missing_prompt_hooks:
                    status.issues.append(
                        "Git 快照 Hook 未注册: "
                        + ", ".join(missing_prompt_hooks)
                        + "；请重新安装/修复远端自动续跑"
                    )
            if needs_error_hook and not error_hook_registered:
                status.issues.append("ResponseError Hook 未注册；请重新安装/修复远端自动续跑")
            if needs_permission_hooks:
                missing_permission_hooks = []
                if not pre_tool_hook_registered:
                    missing_permission_hooks.append("PreToolUse")
                if not permission_hook_registered:
                    missing_permission_hooks.append("PermissionRequest")
                if missing_permission_hooks:
                    status.issues.append(
                        "权限自动确认 Hook 未注册: "
                        + ", ".join(missing_permission_hooks)
                        + "；可能仍会弹 yes，请重新安装/修复远端自动续跑"
                    )
            permissions = (
                provider_config.get("permissions")
                if isinstance(provider_config.get("permissions"), dict)
                else {}
            )
            status.permission_mode = str(permissions.get("defaultMode") or "")
            if parsed_settings and parsed_settings.auto_approve_permission_requests:
                desired_rules = permission_rules_from_auto_settings(parsed_settings)
                missing_rules = missing_allow_rules(desired_rules, permissions.get("allow", []))
                ask_conflicts = conflicting_permission_rules(desired_rules, permissions.get("ask", []))
                deny_conflicts = conflicting_permission_rules(desired_rules, permissions.get("deny", []))
                auto_approves_everything = any(
                    str(tool or "").strip() == "*"
                    for tool in parsed_settings.auto_approve_tools
                )
                broad_deny_rules = []
                if auto_approves_everything:
                    deny_conflict_keys = {rule.casefold() for rule in deny_conflicts}
                    broad_deny_rules = [
                        rule
                        for rule in rules_from_payload(permissions.get("deny", []))
                        if rule.casefold() not in deny_conflict_keys
                    ]
                if status.permission_mode != "dontAsk":
                    status.issues.append(
                        "Claude 权限模式未切到 dontAsk，可能仍会弹 yes；请重新安装/修复远端自动续跑"
                    )
                if missing_rules:
                    status.issues.append("权限 allow 未预授权: " + ", ".join(missing_rules[:5]))
                if ask_conflicts:
                    status.issues.append("permissions.ask 仍会强制询问: " + ", ".join(ask_conflicts[:5]))
                if deny_conflicts:
                    status.issues.append("permissions.deny 会阻止自动执行: " + ", ".join(deny_conflicts[:5]))
                if broad_deny_rules:
                    status.issues.append("permissions.deny 会阻止通配自动执行: " + ", ".join(broad_deny_rules[:5]))
        else:
            status.issues.append("Claude settings.json 无法读取")
    else:
        try:
            hooks = _read_json(client, paths.codex_hooks_path or "", default={}, strict=True)
        except Exception as e:
            hooks = None
            status.issues.append(f"Codex hooks.json 读取失败: {e}")
        if isinstance(hooks, dict):
            stop_hook_registered = any(_is_our_command(command) for command in _iter_codex_hook_commands(hooks, "Stop"))
            prompt_hook_registered = any(
                _is_our_command(command)
                for command in _iter_codex_hook_commands(hooks, "UserPromptSubmit")
            )
            session_hook_registered = any(
                _is_our_command(command)
                for command in _iter_codex_hook_commands(hooks, "SessionStart")
            )
            error_hook_registered = any(_is_our_command(command) for command in _iter_codex_hook_commands(hooks, "Error"))
            needs_stop_hook = bool(status.enabled or status.training_auto_continue_enabled or status.git_snapshot_enabled)
            needs_prompt_snapshot_hooks = bool(status.git_snapshot_enabled)
            needs_error_hook = bool(status.error_recovery_enabled)
            if needs_stop_hook or needs_prompt_snapshot_hooks or needs_error_hook:
                status.hook_registered = (
                    (not needs_stop_hook or stop_hook_registered)
                    and (
                        not needs_prompt_snapshot_hooks
                        or (prompt_hook_registered and session_hook_registered)
                    )
                    and (not needs_error_hook or error_hook_registered)
                )
            else:
                status.hook_registered = (
                    stop_hook_registered
                    or prompt_hook_registered
                    or session_hook_registered
                    or error_hook_registered
                )
            if needs_prompt_snapshot_hooks:
                missing_prompt_hooks = []
                if not prompt_hook_registered:
                    missing_prompt_hooks.append("UserPromptSubmit")
                if not session_hook_registered:
                    missing_prompt_hooks.append("SessionStart")
                if missing_prompt_hooks:
                    status.issues.append(
                        "Git 快照 Hook 未注册: "
                        + ", ".join(missing_prompt_hooks)
                        + "；请重新安装/修复远端自动续跑"
                    )
            if needs_error_hook and not error_hook_registered:
                status.issues.append("Error Hook 未注册；请重新安装/修复远端自动续跑")
        else:
            status.issues.append("Codex hooks.json 无法读取")
        try:
            config = _read_toml(client, paths.provider_config_path, strict=False)
        except Exception as e:
            config = {}
            status.issues.append(f"Codex config.toml 读取失败: {e}")
        status.codex_hooks_enabled = _codex_hooks_enabled_from_config(config)

    hook_required = (
        status.enabled
        or status.training_auto_continue_enabled
        or status.git_snapshot_enabled
        or status.permission_auto_approve_enabled
        or status.error_recovery_enabled
    )

    if hook_required and not status.hook_script_exists:
        status.issues.append("Hook 脚本缺失")
    if hook_required and status.hook_script_matches_expected is False:
        status.issues.append("Hook 脚本与当前版本不一致；请一键修复")
    if hook_required and not status.hook_registered:
        status.issues.append("Hook 未注册")
    if provider == "codex" and hook_required and not status.codex_hooks_enabled:
        status.issues.append("config.toml 未开启 codex_hooks")
    if status.enabled and not status.guidance_installed:
        status.issues.append("指导文件未安装")

    return status
