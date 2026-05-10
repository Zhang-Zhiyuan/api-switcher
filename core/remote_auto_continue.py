"""Install and inspect auto-continue hooks on SSH servers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import posixpath
import shlex
from typing import Any

from core import profile_manager, remote_config
from core.auto_continue.manager import auto_continue_manager
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
    runtime_ready: bool = False
    codex_hooks_enabled: bool | None = None
    issues: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return "Claude" if self.provider_name == "claude" else "Codex"

    @property
    def ready(self) -> bool:
        return (
            self.enabled
            and self.hook_script_exists
            and self.hook_registered
            and self.settings_valid
            and self.runtime_ready
            and (self.codex_hooks_enabled is not False)
        )

    def summary(self) -> str:
        state = "正常" if self.ready else "需处理"
        parts = [
            f"{self.label}: {state}",
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
        "fi; printf '\\n'"
    )
    stdout, _stderr = ssh_manager.execute_command(client, command, timeout=10)
    result = {"os": "unknown", "sh": "", "python": "", "is_posix": False}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    os_name = result.get("os", "unknown").lower()
    result["is_posix"] = any(token in os_name for token in ["linux", "darwin", "freebsd", "openbsd", "netbsd"])
    return result


def _ensure_runtime_ready(env: dict) -> None:
    if not env.get("is_posix"):
        raise RuntimeError(f"远端自动续跑当前仅支持 POSIX/Linux SSH 环境，检测到: {env.get('os') or 'unknown'}")
    if not env.get("sh"):
        raise RuntimeError("远端缺少 sh，无法运行自动续跑 hook")
    if not env.get("python"):
        raise RuntimeError("远端缺少 Python 3.6+，无法运行自动续跑判断逻辑")


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


def save_state(path, data):
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def write_jsonl(path, data):
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        log(f"Failed to write log: {exc}", "WARN")


def run_git_snapshot():
    if not shutil.which("git"):
        return

    def run(args, timeout=15):
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)

    try:
        if run(["git", "rev-parse", "--git-dir"]).returncode != 0:
            run(["git", "init"])

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
        if not username.stdout.strip():
            run(["git", "config", "user.name", "API-Switcher-Auto"], timeout=5)
            run(["git", "config", "user.email", "auto@api-switcher.local"], timeout=5)

        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        run(["git", "commit", "-m", f"[auto-continue] {stamp}"], timeout=30)
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

    if not as_bool(settings.get("enabled"), False):
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

    if any_pattern(settings.get("blocker_patterns"), last_message):
        return

    incomplete_patterns = settings.get("incomplete_patterns")
    if not any_pattern(incomplete_patterns, last_message):
        return

    session_id = (
        data.get("session_id")
        or data.get("sessionId")
        or data.get("conversation_id")
        or data.get("transcript_path")
        or data.get("transcriptPath")
        or os.getcwd()
    )
    hook_event = data.get("hook_event_name") or "Stop"
    agent_id = data.get("agent_id") or data.get("agentId") or ""
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

        if as_bool(settings.get("git_auto_snapshot"), True) and as_bool(settings.get("git_snapshot_on_start"), True):
            run_git_snapshot()

        continuation_prompt = settings.get("continuation_prompt") or "Please continue from where you left off. Complete any remaining work."
        write_jsonl(
            log_path,
            {
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "session_id": str(session_id),
                "hook_event": str(hook_event),
                "agent_id": str(agent_id),
                "decision": "block_stop",
                "reason": "incomplete_work_detected",
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


def _iter_claude_hook_commands(settings: dict, event_names: tuple[str, ...] = ("Stop", "SubagentStop")):
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


def _register_claude_hook(client, paths: RemoteAutoContinuePaths, command: str, apply_to_subagents: bool) -> None:
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

    _write_json(client, paths.provider_config_path, settings)


def _unregister_claude_hook(client, paths: RemoteAutoContinuePaths) -> None:
    settings = _read_json(client, paths.provider_config_path, default={}, strict=False)
    if not isinstance(settings, dict):
        return
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return

    changed = False
    for event_name in ("Stop", "SubagentStop"):
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
            remaining = [h for h in hook_list if not _is_our_command(h.get("command", "") if isinstance(h, dict) else "")]
            if len(remaining) != len(hook_list):
                changed = True
            if remaining:
                new_group = dict(group)
                new_group["hooks"] = remaining
                filtered_groups.append(new_group)
        hooks[event_name] = filtered_groups

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


def _register_codex_hook(client, paths: RemoteAutoContinuePaths, command: str) -> None:
    if not paths.codex_hooks_path:
        raise RuntimeError("Codex hooks 路径缺失")
    hooks = _read_json(client, paths.codex_hooks_path, default={})
    if not isinstance(hooks, dict):
        hooks = {}
    hooks["Stop"] = {"command": command, "timeout": 10}
    _write_json(client, paths.codex_hooks_path, hooks)

    config = _read_toml(client, paths.provider_config_path)
    config["codex_hooks"] = True
    _write_toml(client, paths.provider_config_path, config)


def _unregister_codex_hook(client, paths: RemoteAutoContinuePaths) -> None:
    if not paths.codex_hooks_path:
        return
    hooks = _read_json(client, paths.codex_hooks_path, default={}, strict=False)
    if isinstance(hooks, dict) and "Stop" in hooks:
        if any(_is_our_command(command) for command in _iter_codex_hook_commands(hooks, "Stop")):
            hooks.pop("Stop", None)
            _write_json(client, paths.codex_hooks_path, hooks)


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
    ssh_profile, client = _connect(ssh_name)
    env = _probe_remote_environment(client)
    _ensure_runtime_ready(env)

    paths = _paths(client, ssh_profile, provider)
    resolved_settings = _load_local_settings(provider, settings)
    snapshot_paths = [
        paths.settings_path,
        paths.script_path,
        paths.guidance_path,
        paths.provider_config_path,
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
            _register_claude_hook(client, paths, command, resolved_settings.apply_to_subagents)
        else:
            _register_codex_hook(client, paths, command)
    except Exception:
        _restore_remote_files(client, snapshots)
        raise

    logger.info(f"Installed remote auto-continue for {provider} on {ssh_profile.host}")
    return f"已在 {ssh_profile.host} 安装/修复 {_provider_label(provider)} 远端自动续跑"


def pause_remote_auto_continue(ssh_name: str, provider_name: str) -> str:
    """Disable remote auto-continue while keeping script/settings on the server."""
    provider = _normal_provider(provider_name)
    ssh_profile, client = _connect(ssh_name)
    paths = _paths(client, ssh_profile, provider)

    settings = _read_json(client, paths.settings_path, default={}, strict=False)
    if isinstance(settings, dict):
        settings["enabled"] = False
        _write_json(client, paths.settings_path, settings)

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

    if not status.hook_script_exists:
        status.issues.append("Hook 脚本缺失")
    if not status.hook_registered:
        status.issues.append("Hook 未注册")
    if provider == "codex" and not status.codex_hooks_enabled:
        status.issues.append("config.toml 未开启 codex_hooks")
    if not status.guidance_installed:
        status.issues.append("指导文件未安装")

    return status
