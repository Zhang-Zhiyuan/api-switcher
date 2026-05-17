"""Install and inspect auto-continue hooks on SSH servers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import posixpath
import shlex
from typing import Any

from core import profile_manager, remote_config, security
from core.auto_continue.manager import auto_continue_manager
from core.auto_continue.permission_rules import (
    apply_managed_permission_rules,
    permission_rules_from_auto_settings,
    rules_from_payload,
    rules_payload,
)
from core.ssh_manager import ssh_manager
from models.auto_continue import AutoContinueSettings

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
    git_snapshot_enabled: bool = False
    permission_auto_approve_enabled: bool = False
    git_available: bool = False
    runtime_ready: bool = False
    codex_hooks_enabled: bool | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "Claude" if self.provider_name == "claude" else "Codex"

    @property
    def ready(self) -> bool:
        hook_required = self.enabled or self.git_snapshot_enabled or self.permission_auto_approve_enabled
        return (
            hook_required
            and self.hook_script_exists
            and self.hook_registered
            and self.settings_valid
            and self.runtime_ready
            and (self.codex_hooks_enabled is not False)
            and (not self.git_snapshot_enabled or self.git_available)
        )

    def summary(self) -> str:
        state = "正常" if self.ready else "需处理"
        parts = [
            f"{self.label}: {state}",
            f"Git snapshot {'on' if self.git_snapshot_enabled else 'off'}",
            f"权限自动确认 {'on' if self.permission_auto_approve_enabled else 'off'}",
            f"Git {'ok' if self.git_available else 'missing'}",
            f"系统 {self.remote_os or 'unknown'}",
            f"状态 {'已启用' if self.enabled else '未启用'}",
            f"脚本 {'存在' if self.hook_script_exists else '缺失'}",
            f"Hook {'已注册' if self.hook_registered else '未注册'}",
            f"设置 {'有效' if self.settings_valid else '缺失/无效'}",
        ]
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
    copied.git_auto_snapshot = True
    copied.git_snapshot_on_start = True
    if provider == "codex":
        copied.apply_to_subagents = False
    valid, error = copied.validate()
    if not valid:
        raise ValueError(f"Git snapshot settings invalid: {error}")
    return copied


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


def any_pattern(patterns, text):
    if not isinstance(patterns, list):
        return False
    for pattern in patterns:
        try:
            if re.search(str(pattern), text):
                return True
        except re.error as exc:
            log(f"Invalid pattern ignored: {pattern}: {exc}", "WARN")
    return False


RECOVERABLE_API_ERROR_PATTERNS = [
    r"error running remote compact task",
    r"stream disconnected before completion",
    r"reconnecting\.\.\.\s*\d+/\d+",
    r"upstream connect error",
    r"disconnect/reset before headers",
    r"reset reason.*connection termination",
    r"error sending request for url",
    r"backend-api/codex/responses/compact",
    r"responses/compact",
]


DEFAULT_PERMISSION_AUTO_APPROVE_TOOLS = ["Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"]


def is_recoverable_api_error(text):
    for pattern in RECOVERABLE_API_ERROR_PATTERNS:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        except re.error as exc:
            log(f"Invalid recoverable API error pattern ignored: {pattern}: {exc}", "WARN")
    return False


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
        if allowed == "*" or allowed.lower() == tool_name.lower():
            return True
        if "*" in allowed:
            pattern = "^" + re.escape(allowed).replace("\\*", ".*") + "$"
            try:
                if re.match(pattern, tool_name, re.IGNORECASE):
                    return True
            except re.error:
                continue
    return False


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


def ensure_gitignore():
    path = os.path.join(os.getcwd(), ".gitignore")
    if os.path.exists(path):
        return
    try:
        write_text_atomic(path, "\n".join(DEFAULT_GITIGNORE_LINES) + "\n")
        log("Created local .gitignore for Git snapshots")
    except Exception as exc:
        log(f"Failed to create local .gitignore: {exc}", "WARN")


def run_git_snapshot():
    if not shutil.which("git"):
        return

    def run(args, timeout=15):
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)

    try:
        initialized_repo = False
        if run(["git", "rev-parse", "--git-dir"]).returncode != 0:
            initialized_repo = run(["git", "init"]).returncode == 0

        if initialized_repo:
            ensure_gitignore()

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        if not status.stdout.strip():
            return

        run(["git", "add", "-A"], timeout=30)
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
        commit = run(["git", "commit", "-m", f"[git-snapshot] {stamp}"], timeout=30)
        if commit.returncode != 0:
            log("Git snapshot commit did not complete", "WARN")
    except Exception as exc:
        log(f"Git snapshot failed: {exc}", "WARN")


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

    if as_bool(settings.get("git_auto_snapshot"), True) and as_bool(settings.get("git_snapshot_on_start"), True):
        run_git_snapshot()

    if not as_bool(settings.get("enabled"), False) and not auto_approve_enabled:
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

    is_claude = data.get("hook_event_name") is not None
    hook_event = data.get("hook_event_name") or "Stop"
    agent_id = data.get("agent_id") or data.get("agentId") or ""
    session_id = (
        data.get("session_id")
        or data.get("sessionId")
        or data.get("conversation_id")
        or data.get("transcript_path")
        or data.get("transcriptPath")
        or os.getcwd()
    )

    if is_claude and hook_event == "PermissionRequest":
        if not auto_approve_enabled:
            return
        permission_request = data.get("permission_request") if isinstance(data.get("permission_request"), dict) else {}
        request = data.get("request") if isinstance(data.get("request"), dict) else {}
        tool_name = str(
            data.get("tool_name")
            or data.get("toolName")
            or data.get("tool")
            or permission_request.get("tool_name")
            or request.get("tool_name")
            or ""
        ).strip()
        if not tool_allowed(tool_name, permission_tools(settings)):
            return

        max_auto_approvals = as_int(settings.get("auto_approve_max_per_session"), 3)
        state_seed = f"{session_id}|PermissionRequest|{agent_id}"
        state_key = hashlib.sha256(state_seed.encode("utf-8", errors="replace")).hexdigest()
        os.makedirs(state_dir, exist_ok=True)
        state_path = os.path.join(state_dir, "auto_continue_stop_state.json")
        log_path = os.path.join(state_dir, "auto_continue_stop_log.jsonl")
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
            return

        try:
            state = load_state(state_path)
            count = as_int(state.get(state_key), 0)
            if max_auto_approvals > 0 and count >= max_auto_approvals:
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
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "allow",
                        "updatedPermissions": [
                            {
                                "type": "addRules",
                                "rules": [{"toolName": tool_name}],
                                "behavior": "allow",
                                "destination": "session",
                            }
                        ],
                    },
                }
            }, ensure_ascii=False))
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
        return

    if not as_bool(settings.get("enabled"), False):
        return

    last_message = pick_text(
        data,
        ["last_assistant_message", "last_message", "assistant_message", "message", "content", "text"],
    )
    if not last_message:
        transcript_path = data.get("transcript_path") or data.get("transcriptPath")
        last_message = transcript_tail(transcript_path)
    if not last_message.strip():
        return

    max_continuations = as_int(settings.get("max_continuations"), 3)
    if max_continuations <= 0:
        return

    if is_claude and as_bool(settings.get("conservative_mode"), True) and as_bool(data.get("stop_hook_active"), False):
        return

    recoverable_api_error = is_recoverable_api_error(last_message)

    if any_pattern(settings.get("blocker_patterns"), last_message) and not recoverable_api_error:
        return

    incomplete_patterns = settings.get("incomplete_patterns")
    if not any_pattern(incomplete_patterns, last_message) and not recoverable_api_error:
        return

    state_seed = f"{session_id}|{hook_event}|{agent_id}"
    state_key = hashlib.sha256(state_seed.encode("utf-8", errors="replace")).hexdigest()

    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "auto_continue_stop_state.json")
    log_path = os.path.join(state_dir, "auto_continue_stop_log.jsonl")
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
        log("Failed to acquire state lock", "WARN")
        return

    try:
        state = load_state(state_path)
        count = as_int(state.get(state_key), 0)
        if count >= max_continuations:
            write_jsonl(
                log_path,
                {
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "session_id": str(session_id),
                    "hook_event": str(hook_event),
                    "agent_id": str(agent_id),
                    "decision": "allow_stop",
                    "reason": "max_continuations_reached",
                    "count": count,
                },
            )
            return

        count += 1
        state[state_key] = count
        save_state(state_path, state)

        continuation_prompt = settings.get("continuation_prompt") or "Please continue from where you left off. Complete any remaining work."
        continue_reason = "recoverable_api_error_detected" if recoverable_api_error else "incomplete_work_detected"
        write_jsonl(
            log_path,
            {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "session_id": str(session_id),
                "hook_event": str(hook_event),
                "agent_id": str(agent_id),
                "decision": "block_stop",
                "reason": continue_reason,
                "count": count,
                "continuation_prompt": continuation_prompt,
            },
        )

        if is_claude:
            output = {"decision": "block", "reason": continuation_prompt, "suppressOutput": True}
        else:
            output = {"continue": True, "message": continuation_prompt}
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
    return header + body


def _is_our_command(command: str) -> bool:
    return any(marker in str(command or "") for marker in SCRIPT_MARKERS)


def _iter_claude_hook_commands(settings: dict, event_names: tuple[str, ...] = ("Stop", "SubagentStop", "PermissionRequest")):
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


def _read_managed_permission_rules(client, path: str) -> list[str]:
    payload = _read_json(client, path, default={}, strict=False)
    return rules_from_payload(payload)


def _write_managed_permission_rules(client, path: str, rules: list[str]) -> None:
    if rules:
        _write_json(client, path, rules_payload(rules), mode=0o600)
    else:
        _remove_remote_file(client, path)


def _register_claude_hook(
    client,
    paths: RemoteAutoContinuePaths,
    command: str,
    apply_to_subagents: bool,
    settings_data: AutoContinueSettings | None = None,
) -> None:
    settings = _read_json(client, paths.provider_config_path, default={})
    if not isinstance(settings, dict):
        settings = {}
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

    register_event("Stop", hook_def)
    if apply_to_subagents:
        subagent_hook = dict(hook_def)
        subagent_hook["statusMessage"] = "Checking whether Claude subagent should continue"
        register_event("SubagentStop", subagent_hook)
    else:
        register_event("SubagentStop", None)
    if settings_data and settings_data.auto_approve_permission_requests:
        permission_hook = dict(hook_def)
        permission_hook["statusMessage"] = "Auto-approving configured Claude permission request if allowed"
        register_event("PermissionRequest", permission_hook)
    else:
        register_event("PermissionRequest", None)

    previous_rules = _read_managed_permission_rules(client, paths.permission_rules_path)
    desired_rules = permission_rules_from_auto_settings(settings_data)
    settings, managed_rules = apply_managed_permission_rules(settings, desired_rules, previous_rules)

    _write_json(client, paths.provider_config_path, settings)
    _write_managed_permission_rules(client, paths.permission_rules_path, managed_rules)


def _unregister_claude_hook(client, paths: RemoteAutoContinuePaths) -> None:
    settings = _read_json(client, paths.provider_config_path, default={}, strict=False)
    if not isinstance(settings, dict):
        return
    hooks = settings.get("hooks", {})

    changed = False
    if isinstance(hooks, dict):
        for event_name in ("Stop", "SubagentStop", "PermissionRequest"):
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

    previous_rules = _read_managed_permission_rules(client, paths.permission_rules_path)
    if previous_rules:
        settings, _managed_rules = apply_managed_permission_rules(settings, [], previous_rules)
        _write_managed_permission_rules(client, paths.permission_rules_path, [])
        changed = True

    if changed:
        _write_json(client, paths.provider_config_path, settings)


def _iter_codex_hook_commands(hooks: dict, event_name: str):
    value = hooks.get(event_name)
    if isinstance(value, dict):
        command = value.get("command")
        if command:
            yield str(command)
        for hook in value.get("hooks", []) if isinstance(value.get("hooks"), list) else []:
            if isinstance(hook, dict) and hook.get("command"):
                yield str(hook["command"])
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                command = item.get("command")
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
        return [dict(hook) for hook in value if isinstance(hook, dict) and hook.get("command")]
    return []


def _format_codex_event_hooks(hook_list: list[dict]):
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
    for value in hooks.values():
        if isinstance(value, dict):
            if value.get("command"):
                return True
            hook_list = value.get("hooks")
            if isinstance(hook_list, list) and any(isinstance(hook, dict) and hook.get("command") for hook in hook_list):
                return True
        elif isinstance(value, list):
            if any(isinstance(item, dict) and item.get("command") for item in value):
                return True
    return False


def _set_codex_hooks_enabled(client, paths: RemoteAutoContinuePaths, enabled: bool) -> None:
    config = _read_toml(client, paths.provider_config_path, strict=False)
    if enabled:
        config["codex_hooks"] = True
    elif config.get("codex_hooks") is not None:
        config["codex_hooks"] = False
    _write_toml(client, paths.provider_config_path, config)


def _register_codex_hook(client, paths: RemoteAutoContinuePaths, command: str) -> None:
    if not paths.codex_hooks_path:
        raise RuntimeError("Codex hooks 路径缺失")
    hooks = _read_json(client, paths.codex_hooks_path, default={})
    if not isinstance(hooks, dict):
        hooks = {}
    _upsert_codex_event_hook(hooks, "Stop", {"command": command, "timeout": 10})
    _write_json(client, paths.codex_hooks_path, hooks)

    _set_codex_hooks_enabled(client, paths, True)


def _unregister_codex_hook(client, paths: RemoteAutoContinuePaths) -> None:
    if not paths.codex_hooks_path:
        return
    hooks = _read_json(client, paths.codex_hooks_path, default={}, strict=False)
    if isinstance(hooks, dict) and "Stop" in hooks:
        if _remove_codex_event_hook(hooks, "Stop"):
            _write_json(client, paths.codex_hooks_path, hooks)
            if not _codex_hooks_has_entries(hooks):
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
        require_git=bool(resolved_settings.git_auto_snapshot and resolved_settings.git_snapshot_on_start),
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
            _register_codex_hook(client, paths, command)
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
            _register_codex_hook(client, paths, command)
    except Exception:
        _restore_remote_files(client, snapshots)
        raise

    logger.info(f"Installed remote Git snapshot hook for {provider} on {ssh_profile.host}")
    return f"已在 {ssh_profile.host} 安装 {_provider_label(provider)} 远端 Git 快照 Hook"


def pause_remote_auto_continue(ssh_name: str, provider_name: str) -> str:
    """Disable remote auto-continue while keeping script/settings on the server."""
    provider = _normal_provider(provider_name)
    ssh_profile, client = _connect(ssh_name)
    paths = _paths(client, ssh_profile, provider)

    settings = _read_json(client, paths.settings_path, default={}, strict=False)
    keep_hook = False
    if isinstance(settings, dict):
        settings["enabled"] = False
        keep_hook = bool(
            settings.get("git_auto_snapshot", True) and settings.get("git_snapshot_on_start", True)
        ) or bool(provider == "claude" and settings.get("auto_approve_permission_requests"))
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

    try:
        settings = _read_json(client, paths.settings_path, default=None, strict=False)
    except Exception as e:
        settings = None
        status.issues.append(f"设置读取失败: {e}")
    if isinstance(settings, dict):
        try:
            parsed = AutoContinueSettings.from_dict(settings)
            status.settings_valid = True
            status.enabled = parsed.enabled
            status.git_snapshot_enabled = bool(parsed.git_auto_snapshot and parsed.git_snapshot_on_start)
            status.permission_auto_approve_enabled = bool(
                provider == "claude" and parsed.auto_approve_permission_requests
            )
            if status.git_snapshot_enabled and not env.get("git"):
                status.issues.append("缺少 git")
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
            provider_config = _read_json(client, paths.provider_config_path, default={}, strict=False)
        except Exception as e:
            provider_config = None
            status.issues.append(f"Claude settings.json 读取失败: {e}")
        if isinstance(provider_config, dict):
            status.hook_registered = any(_is_our_command(command) for command in _iter_claude_hook_commands(provider_config))
        else:
            status.issues.append("Claude settings.json 无法读取")
    else:
        try:
            hooks = _read_json(client, paths.codex_hooks_path or "", default={}, strict=False)
        except Exception as e:
            hooks = None
            status.issues.append(f"Codex hooks.json 读取失败: {e}")
        if isinstance(hooks, dict):
            status.hook_registered = any(_is_our_command(command) for command in _iter_codex_hook_commands(hooks, "Stop"))
        else:
            status.issues.append("Codex hooks.json 无法读取")
        try:
            config = _read_toml(client, paths.provider_config_path, strict=False)
        except Exception as e:
            config = {}
            status.issues.append(f"Codex config.toml 读取失败: {e}")
        status.codex_hooks_enabled = bool(config.get("codex_hooks"))

    hook_required = status.enabled or status.git_snapshot_enabled or status.permission_auto_approve_enabled

    if hook_required and not status.hook_script_exists:
        status.issues.append("Hook 脚本缺失")
    if hook_required and not status.hook_registered:
        status.issues.append("Hook 未注册")
    if provider == "codex" and hook_required and not status.codex_hooks_enabled:
        status.issues.append("config.toml 未开启 codex_hooks")
    if status.enabled and not status.guidance_installed:
        status.issues.append("指导文件未安装")

    return status
