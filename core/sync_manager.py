import hashlib
import json
import logging
import posixpath
import re
import shlex
from copy import deepcopy
from dataclasses import dataclass
from core import auth_parser, parser, profile_manager, remote_config, remote_proxy, security, toml_parser, vscode_parser
from core.providers import ProviderRegistry
from core.ssh_manager import ssh_manager
from core.url_validation import validate_api_base_url

logger = logging.getLogger(__name__)


ROOT_BYPASS_ADJUSTED_MESSAGE = (
    "已兼容 root 登录：Claude Code 禁止 root/sudo 使用 "
    "bypassPermissions（--dangerously-skip-permissions），已自动将远端权限模式改为 dontAsk。"
)

CLAUDE_BYPASS_PERMISSION_MODE = "bypassPermissions"
CLAUDE_ROOT_SAFE_PERMISSION_MODE = "dontAsk"


@dataclass(frozen=True)
class RemoteWireBenchmarkResult:
    success: bool
    recommended_wire_api: str | None = None
    selected_model: str = ""
    summary: str = ""
    error: str = ""


@dataclass(frozen=True)
class RemoteConfigCandidate:
    kind: str
    label: str
    category: str = "api"
    product: str = ""
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    base_url: str = ""
    has_api_key: bool = False
    importable: bool = False
    reason: str = ""
    paths: tuple[str, ...] = ()

    def display_name(self) -> str:
        status = "可拉取" if self.importable else "跳过"
        details = []
        if self.provider_label:
            details.append(self.provider_label)
        if self.model:
            details.append(self.model)
        suffix = " / ".join(details) if details else self.reason
        return f"{self.label} [{status}]" + (f" - {suffix}" if suffix else "")

    def summary(self) -> str:
        if self.category == "account":
            key_state = "有登录态" if self.has_api_key else "无登录态"
        else:
            key_state = "有密钥" if self.has_api_key else "无密钥"
        detail = self.reason or key_state
        return f"{self.label}: {detail}"


CODEX_WIRE_API_AUTO = "auto"
CODEX_WIRE_API_PROFILE = "profile"
CODEX_WIRE_API_VALUES = {"responses"}
CODEX_WIRE_API_MODES = CODEX_WIRE_API_VALUES | {CODEX_WIRE_API_AUTO, CODEX_WIRE_API_PROFILE}
REMOTE_API_CLEAR_TARGETS = {"claude", "codex", "all"}
REMOTE_CLAUDE_API_ENV_NAMES = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
)
REMOTE_CODEX_API_ENV_NAMES = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "MINIMAX_API_KEY",
    "DASHSCOPE_API_KEY",
    "GEMINI_API_KEY",
    "ZHIPUAI_API_KEY",
)


_REMOTE_CODEX_WIRE_BENCHMARK_SCRIPT = r"""
import hashlib
import json
import os
import pathlib
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def classify_transport_error(error, streaming=False):
    text = (type(error).__name__ + ": " + str(error))[:180]
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout before completion: " + text[:140]
    if streaming and any(
        marker in lowered
        for marker in (
            "incomplete read",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "remote end closed",
            "server disconnected",
            "stream disconnected",
        )
    ):
        return "stream disconnected before completion: " + text[:140]
    return text[:160]


def openai_url(base_url, resource):
    base_url = (base_url or "https://api.openai.com/v1").strip().rstrip("/")
    if "://" not in base_url:
        base_url = "https://" + base_url
    parsed = urllib.parse.urlparse(base_url)
    path = parsed.path.rstrip("/")
    resource = resource.strip("/")
    if path.endswith(("/v1", "/v4")):
        new_path = path + "/" + resource
    elif parsed.netloc.lower() == "api.openai.com":
        new_path = (path + "/v1/" + resource) if path else ("/v1/" + resource)
    else:
        new_path = (path + "/" + resource) if path else ("/" + resource)
    return urllib.parse.urlunparse(parsed._replace(path=new_path))


def validated_privacy_mode(config_path, validation_token):
    # The caller validates the complete strict YAML contract before sending any
    # credential. Re-read the same config here and require its SHA-256 to match,
    # preventing a drifted replacement between the SFTP check and this request.
    token = str(validation_token or "")
    if token == "absent":
        try:
            if pathlib.Path(config_path).exists():
                raise ValueError("managed proxy config changed after validation")
        except ValueError:
            raise
        except Exception as error:
            raise RuntimeError("cannot verify managed proxy config absence: " + str(error)[:120]) from error
        return False

    mode, separator, expected_hash = token.partition("-sha256:")
    if not separator or mode not in {"compat", "strict"} or not re.fullmatch(
        r"[0-9a-f]{64}", expected_hash
    ):
        raise ValueError("managed proxy validation token is invalid")
    try:
        text = pathlib.Path(config_path).read_text(encoding="utf-8-sig", errors="replace")
    except Exception as error:
        raise RuntimeError("cannot read validated managed proxy config: " + str(error)[:120]) from error
    actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError("managed proxy config changed after validation")
    lines = {line.strip() for line in text.splitlines()}
    strict_marker = "# API-Switcher-Strict-Privacy: application-layer"
    managed_marker = "# Managed by API切换器 AI proxy"
    if mode == "strict":
        if strict_marker not in lines or managed_marker not in lines:
            raise ValueError("validated strict managed proxy markers are missing")
        return True
    if strict_marker in lines:
        raise ValueError("strict managed proxy appeared after compatibility validation")
    return False


def managed_proxy_url(env_path):
    # Read only the managed loopback proxy assignment; never source shell code.
    if not env_path:
        return ""
    path = pathlib.Path(env_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as error:
        raise RuntimeError("cannot read managed proxy environment: " + str(error)[:120]) from error

    if "# Managed by API切换器." not in text:
        raise ValueError("managed proxy environment marker is missing")
    values = []
    for raw_line in text.splitlines():
        try:
            parts = shlex.split(raw_line, comments=True, posix=True)
        except ValueError as error:
            raise ValueError("managed proxy environment contains invalid shell quoting") from error
        if len(parts) != 2 or parts[0] != "export":
            continue
        name, separator, value = parts[1].partition("=")
        if separator and name == "API_SWITCHER_AI_PROXY_URL":
            values.append(value)
    if len(values) != 1:
        raise ValueError("managed proxy environment must contain one proxy URL")

    parsed = urllib.parse.urlsplit(values[0])
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("managed proxy port is invalid") from error
    if (
        parsed.scheme.lower() != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("managed proxy URL must be an HTTP IPv4 loopback endpoint")
    return f"http://127.0.0.1:{port}"


def network_opener(proxy_url):
    # ProxyHandler consults NO_PROXY even when given an explicit proxy map.
    # Remove both spellings so NO_PROXY='*' cannot silently bypass the managed
    # loopback proxy.  An empty handler is used only when no managed env file
    # exists; there is no retry or direct fallback after a proxy failure.
    for key in ("NO_PROXY", "no_proxy"):
        os.environ.pop(key, None)
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else {}
    return urllib.request.build_opener(urllib.request.ProxyHandler(proxies))


def call(opener, api_key, base_url, model, wire_api, timeout):
    if wire_api == "responses":
        url = openai_url(base_url, "responses")
        payload = {
            "model": model,
            "input": "Write 40 short words about reliable coding workflows, then write DONE.",
            "max_output_tokens": 96,
            "stream": True,
        }
    else:
        url = openai_url(base_url, "chat/completions")
        payload = {"model": model, "messages": [{"role": "user", "content": "Reply OK only."}], "max_tokens": 8}

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if wire_api == "responses" else "application/json",
        },
        method="POST",
    )
    start = time.time()
    try:
        with opener.open(request, timeout=timeout) as response:
            if wire_api == "responses":
                snippet_parts = []
                snippet_len = 0
                rolling_text = ""
                event_count = 0
                max_events = 1200
                while True:
                    raw_line = response.readline()
                    if not raw_line:
                        break
                    event_count += 1
                    line = raw_line.decode("utf-8", errors="replace")
                    if snippet_len < 160:
                        snippet_parts.append(line)
                        snippet_len += len(line)
                    rolling_text = (rolling_text + line)[-2000:]
                    lowered = rolling_text.lower()
                    if (
                        "event: error" in lowered
                        or "response.failed" in lowered
                        or "response.incomplete" in lowered
                        or re.search(r'"type"\s*:\s*"error"', lowered)
                    ):
                        return {
                            "ok": False,
                            "status": response.status,
                            "ms": round((time.time() - start) * 1000),
                            "error": "stream error: " + "".join(snippet_parts).strip()[:160],
                        }
                    if "response.completed" in lowered or "[done]" in lowered or "event: done" in lowered:
                        return {
                            "ok": 200 <= response.status < 300,
                            "status": response.status,
                            "ms": round((time.time() - start) * 1000),
                            "error": "" if 200 <= response.status < 300 else "HTTP " + str(response.status),
                        }
                    if time.time() - start >= timeout:
                        return {
                            "ok": False,
                            "status": response.status,
                            "ms": round((time.time() - start) * 1000),
                            "error": "stream timed out before completion",
                        }
                    if event_count >= max_events:
                        return {
                            "ok": False,
                            "status": response.status,
                            "ms": round((time.time() - start) * 1000),
                            "error": "stream exceeded event limit before completion",
                        }
                body = "".join(snippet_parts).strip()
                return {
                    "ok": False,
                    "status": response.status,
                    "ms": round((time.time() - start) * 1000),
                    "error": "stream did not complete: " + body[:160],
                }
            body = response.read().decode("utf-8", errors="replace")
            body = body[:300]
            return {
                "ok": 200 <= response.status < 300 and body.lstrip().startswith(("{", "[")),
                "status": response.status,
                "ms": round((time.time() - start) * 1000),
            }
    except urllib.error.HTTPError as error:
        body = error.read(300).decode("utf-8", errors="replace").replace(api_key, "[redacted]")
        return {"ok": False, "status": error.code, "ms": round((time.time() - start) * 1000), "error": body[:160]}
    except Exception as error:
        return {
            "ok": False,
            "status": None,
            "ms": round((time.time() - start) * 1000),
            "error": classify_transport_error(error, streaming=(wire_api == "responses")),
        }


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as error:
        print(json.dumps({"success": False, "error": "invalid payload: " + str(error)[:160]}, ensure_ascii=False))
        return
    try:
        strict_privacy = validated_privacy_mode(
            sys.argv[2] if len(sys.argv) > 2 else "",
            sys.argv[3] if len(sys.argv) > 3 else "",
        )
        proxy_url = managed_proxy_url(sys.argv[1] if len(sys.argv) > 1 else "")
        if strict_privacy and not proxy_url:
            raise ValueError(
                "strict managed proxy is configured but its loopback environment is unavailable"
            )
        opener = network_opener(proxy_url)
    except Exception as error:
        print(json.dumps({
            "success": False,
            "error": "managed proxy configuration invalid: " + str(error)[:160],
            "summaries": [],
        }, ensure_ascii=False))
        return
    api_key = str(payload.get("api_key") or "")
    base_url = str(payload.get("base_url") or "")
    model = str(payload.get("model") or "")
    try:
        timeout = min(30, max(1, int(payload.get("timeout") or 10)))
    except (TypeError, ValueError):
        timeout = 10
    try:
        repeat_count = min(5, max(1, int(payload.get("repeat_count") or 3)))
    except (TypeError, ValueError):
        repeat_count = 3
    wire_apis = payload.get("wire_apis") or ["responses"]

    summaries = []
    best = None
    for wire_api in wire_apis:
        wire_api = str(wire_api or "").strip().lower()
        if wire_api not in {"chat", "responses"}:
            continue
        results = [call(opener, api_key, base_url, model, wire_api, timeout) for _ in range(repeat_count)]
        successes = [item for item in results if item.get("ok")]
        avg_ms = round(sum(item["ms"] for item in successes) / len(successes)) if successes else None
        statuses = ",".join(str(item.get("status") or "-") for item in results)
        errors = [item.get("error") for item in results if item.get("error")]
        summary = {
            "wire_api": wire_api,
            "successes": len(successes),
            "repeat_count": repeat_count,
            "avg_ms": avg_ms,
            "statuses": statuses,
            "error": (errors[-1] if errors else ""),
        }
        summaries.append(summary)
        score = (summary["successes"], -(avg_ms if avg_ms is not None else timeout * 1000))
        if best is None or score > best[0]:
            best = (score, summary)

    recommended = best[1]["wire_api"] if best and best[1]["successes"] > 0 else None
    print(json.dumps({
        "success": recommended is not None,
        "recommended_wire_api": recommended,
        "selected_model": model,
        "summaries": summaries,
    }, ensure_ascii=False))


main()
"""


_SECRET_PATTERN = re.compile(r"(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+")


def _redact_output(text: str) -> str:
    return _SECRET_PATTERN.sub(r"\1***", str(text or "")).strip()


def normalize_codex_wire_api_mode(mode: str | None) -> str:
    value = str(mode or CODEX_WIRE_API_AUTO).strip().lower()
    aliases = {
        "": CODEX_WIRE_API_AUTO,
        "default": CODEX_WIRE_API_AUTO,
        "remote_auto": CODEX_WIRE_API_AUTO,
        "benchmark": CODEX_WIRE_API_AUTO,
        "local": CODEX_WIRE_API_PROFILE,
        "use_profile": CODEX_WIRE_API_PROFILE,
        "chat": "responses",
    }
    value = aliases.get(value, value)
    if value not in CODEX_WIRE_API_MODES:
        raise ValueError(f"不支持的 Codex wire_api 策略: {mode}")
    return value


def _find_profile(profiles: list, name: str, label: str):
    profile = next((p for p in profiles if p.name == name), None)
    if not profile:
        raise ValueError(f"未找到 {label}: {name}")
    return profile


def _connect_ssh(ssh_name: str):
    ssh_profile = _find_profile(profile_manager.list_ssh_profiles(), ssh_name, "SSH 服务器")
    return ssh_profile, ssh_manager.connect(ssh_profile)


def _provider_display_name(provider_id: str) -> str:
    provider = ProviderRegistry.get_provider(provider_id)
    return provider.display_name if provider else (provider_id or "未知")


def _is_root_ssh_user(ssh_profile, client=None) -> bool:
    username = str(getattr(ssh_profile, "username", "") or "").strip().lower()
    if username == "root":
        return True
    if client is None:
        return False
    try:
        _stdin, stdout, _stderr = client.exec_command("id -u 2>/dev/null || true", timeout=5)
        return stdout.read().decode("utf-8", errors="replace").strip() == "0"
    except Exception:
        return False


def _make_claude_settings_root_safe(settings: dict, ssh_profile, client=None) -> tuple[dict, bool]:
    if not _is_root_ssh_user(ssh_profile, client):
        return settings, False

    settings = dict(settings)
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}

    permissions = dict(permissions)
    changed = False

    if permissions.get("defaultMode") != CLAUDE_ROOT_SAFE_PERMISSION_MODE:
        permissions["defaultMode"] = CLAUDE_ROOT_SAFE_PERMISSION_MODE
        changed = True

    if settings.get("skipDangerousModePermissionPrompt") is not False:
        settings["skipDangerousModePermissionPrompt"] = False
        changed = True

    settings["permissions"] = permissions
    return settings, changed


def _make_vscode_settings_root_safe(settings: dict, ssh_profile, client=None) -> tuple[dict, bool]:
    if not _is_root_ssh_user(ssh_profile, client):
        return settings, False

    settings = dict(settings)
    changed = False
    if settings.get("claudeCode.initialPermissionMode") != CLAUDE_ROOT_SAFE_PERMISSION_MODE:
        settings["claudeCode.initialPermissionMode"] = CLAUDE_ROOT_SAFE_PERMISSION_MODE
        changed = True
    if settings.get("claudeCode.allowDangerouslySkipPermissions") is not False:
        settings["claudeCode.allowDangerouslySkipPermissions"] = False
        changed = True
    return settings, changed


def _sync_remote_vscode_root_safety(client, ssh_profile) -> bool:
    if not _is_root_ssh_user(ssh_profile, client):
        return False

    paths = _remote_vscode_paths(client)
    targets = []
    for path in paths:
        settings = remote_config.read_remote_json(client, path, strict=True)
        if settings is not None:
            targets.append((path, settings))
    # Preserve the former behavior: root synchronization creates Stable's
    # Machine settings when no VS Code-family settings file exists yet.
    if not targets and paths:
        targets.append((paths[0], {}))

    any_changed = False
    for path, settings in targets:
        updated, changed = _make_vscode_settings_root_safe(settings, ssh_profile, client)
        if not changed:
            continue
        remote_config.write_remote_json(client, path, updated, file_mode=0o600)
        written = remote_config.read_remote_json(client, path, strict=True)
        if written != updated:
            raise RuntimeError(f"远程 VS Code Claude 权限配置写入后回读不一致: {path}")
        any_changed = True
    return any_changed


def _sync_remote_vscode_official_account(client, ssh_profile, official_model: str) -> bool:
    """Clear third-party Claude extension overrides without cross-file broadcast."""
    paths = _remote_vscode_paths(client)
    targets = []
    for path in paths:
        settings = remote_config.read_remote_json(client, path, strict=True)
        if settings is not None:
            targets.append((path, settings))
    # Root synchronization previously created Stable's settings through the
    # broadcast helper. Keep that behavior, but do not create files for a
    # regular user who has no VS Code-family Machine settings yet.
    if not targets and paths and _is_root_ssh_user(ssh_profile, client):
        targets.append((paths[0], {}))

    any_changed = False
    for path, settings in targets:
        updated = vscode_parser.clear_claude_profile_overrides(settings, official_model)
        if updated == settings:
            continue
        remote_config.write_remote_json(client, path, updated, file_mode=0o600)
        written = remote_config.read_remote_json(client, path, strict=True)
        if written != updated:
            raise RuntimeError(f"远程 VS Code Claude 官方账号配置写入后回读不一致: {path}")
        any_changed = True
    return any_changed


def _codex_profile_api_key(profile) -> str:
    return security.get_secret(getattr(profile, "api_key_ref", None)) or ""


def _codex_profile_runtime_env_keys(profile) -> list[str]:
    env_key = profile.validated_env_key()
    return [env_key] if env_key else []


def _persist_remote_codex_env(client, profile, api_key: str, ssh_profile=None) -> list[str]:
    if not api_key:
        return []
    from core import codex_env, persistent_env

    env_keys = _codex_profile_runtime_env_keys(profile)
    updates = {key: api_key for key in env_keys}
    persistent_env.set_remote_user_env(client, updates)
    current_codex_env = remote_config.read_remote_codex_env(client, ssh_profile) or ""
    remote_config.write_remote_codex_env(
        client,
        codex_env.merge_codex_env_text(current_codex_env, updates=updates),
        ssh_profile,
    )
    return env_keys


def _remote_persistent_env_path(client) -> str:
    from core import persistent_env

    return posixpath.join(remote_config._remote_home(client), persistent_env.REMOTE_ENV_FILENAME)


def _remote_shell_env_paths(client) -> tuple[str, ...]:
    from core import persistent_env

    home = remote_config._remote_home(client)
    return tuple(posixpath.join(home, filename) for filename, _create in persistent_env.REMOTE_SHELL_FILES)


def _remote_vscode_paths(client) -> tuple[str, ...]:
    return tuple(remote_config._expand_remote_path(client, path) for path in remote_config.REMOTE_VSCODE_SETTINGS_PATHS)


def _validate_existing_remote_json_texts(client, snapshot: dict[str, str | None], paths) -> None:
    for path in paths:
        if snapshot.get(path) is not None:
            remote_config.read_remote_json(client, path, strict=True)


def _remote_file_snapshot(client, paths) -> dict[str, str | None]:
    return {path: remote_config.read_remote_text(client, path) for path in dict.fromkeys(paths)}


def _restore_remote_file_snapshot(client, snapshot: dict[str, str | None]) -> list[str]:
    errors = []
    for path, previous in snapshot.items():
        try:
            if previous is None:
                remote_config.delete_remote_file(client, path)
            else:
                remote_config.write_remote_text(client, path, previous, file_mode=0o600)
            if remote_config.read_remote_text(client, path) != previous:
                raise RuntimeError("回滚后回读不一致")
        except Exception as error:
            errors.append(f"{path}: {error}")
    return errors


def _run_remote_transaction(client, snapshot: dict[str, str | None], label: str, operation) -> None:
    try:
        operation()
    except Exception as write_error:
        rollback_errors = _restore_remote_file_snapshot(client, snapshot)
        if rollback_errors:
            raise RuntimeError(f"{label}失败，且回滚不完整: " + "；".join(rollback_errors)) from write_error
        raise


def _strict_remote_read(reader, client, ssh_profile):
    """Use strict readers while tolerating old lightweight test adapters."""
    try:
        return reader(client, ssh_profile, strict=True)
    except TypeError as error:
        message = str(error)
        if "unexpected keyword argument" not in message or "strict" not in message:
            raise
        return reader(client, ssh_profile)


def _validate_remote_claude_api(client, ssh_profile, expected_settings: dict, expected_config: dict) -> None:
    written_settings = _strict_remote_read(remote_config.read_remote_claude_settings, client, ssh_profile) or {}
    written_config = _strict_remote_read(remote_config.read_remote_claude_config, client, ssh_profile) or {}
    if written_settings != expected_settings or written_config != expected_config:
        raise RuntimeError("远程 Claude API 配置写入后回读不一致")


def _validate_remote_claude_account(
    client,
    ssh_profile,
    expected_credentials: dict,
    expected_settings: dict,
    expected_config: dict,
) -> None:
    written_credentials = _strict_remote_read(remote_config.read_remote_claude_credentials, client, ssh_profile) or {}
    written_settings = _strict_remote_read(remote_config.read_remote_claude_settings, client, ssh_profile) or {}
    written_config = _strict_remote_read(remote_config.read_remote_claude_config, client, ssh_profile) or {}
    ok, reason = profile_manager._validate_claude_account_credentials(written_credentials)
    if not ok or written_credentials != expected_credentials:
        raise RuntimeError(f"远程 Claude 账号凭据写入后校验失败: {reason}")
    if written_settings != expected_settings or written_config != expected_config:
        raise RuntimeError("远程 Claude 账号配置写入后回读不一致")


def _validate_remote_codex_api(client, ssh_profile, expected_config: dict, expected_auth: dict) -> None:
    written_config = _strict_remote_read(remote_config.read_remote_codex_config, client, ssh_profile) or {}
    written_auth = _strict_remote_read(remote_config.read_remote_codex_auth, client, ssh_profile) or {}
    if written_config != expected_config or written_auth != expected_auth:
        raise RuntimeError("远程 Codex API 配置写入后回读不一致")


def _validate_remote_codex_env(client, ssh_profile, persistent_env_path: str, env_keys: list[str], api_key: str) -> None:
    if not env_keys:
        return
    from core import codex_env

    codex_values = codex_env.parse_codex_env_text(remote_config.read_remote_codex_env(client, ssh_profile) or "")
    shell_values = codex_env.parse_codex_env_text(remote_config.read_remote_text(client, persistent_env_path) or "")
    for env_key in env_keys:
        if codex_values.get(env_key) != api_key or shell_values.get(env_key) != api_key:
            raise RuntimeError(f"远程 Codex 环境变量 {env_key} 写入后回读校验失败")


def _validate_remote_codex_env_removed(
    client,
    ssh_profile,
    persistent_env_path: str,
    env_names,
) -> None:
    from core import codex_env

    codex_values = codex_env.parse_codex_env_text(remote_config.read_remote_codex_env(client, ssh_profile) or "")
    shell_values = codex_env.parse_codex_env_text(remote_config.read_remote_text(client, persistent_env_path) or "")
    remaining = [name for name in env_names if name in codex_values or name in shell_values]
    if remaining:
        raise RuntimeError(f"远程 Codex API 环境变量清理后回读校验失败: {', '.join(remaining)}")


def _codex_provider_table(config: dict, provider_id: str) -> dict:
    model_providers = config.get("model_providers")
    if not isinstance(model_providers, dict):
        model_providers = {}
        config["model_providers"] = model_providers

    table = model_providers.get(provider_id)
    if not isinstance(table, dict):
        table = {}
        model_providers[provider_id] = table
    return table


def _remote_codex_base_url(config: dict, profile) -> str:
    provider_id = str(config.get("model_provider") or getattr(profile, "model_provider", "") or "").strip()
    table = _codex_provider_table(config, provider_id) if provider_id else {}
    base_url = str(table.get("base_url") or getattr(profile, "custom_base_url", "") or "").strip()
    if base_url:
        return base_url

    provider = ProviderRegistry.get_provider(provider_id)
    return provider.base_url_for_codex() if provider else ""


def _remote_codex_model(config: dict, profile) -> str:
    model = str(config.get("model") or getattr(profile, "model", "") or "").strip()
    if model:
        return model

    provider = ProviderRegistry.get_provider(str(config.get("model_provider") or getattr(profile, "model_provider", "")))
    return (provider.default_model if provider else "") or "gpt-5.5"


def _set_remote_codex_wire_api(config: dict, profile, wire_api: str) -> bool:
    wire_api = str(wire_api or "").strip().lower()
    if wire_api == "chat":
        wire_api = "responses"
    if wire_api != "responses":
        return False

    provider_id = str(config.get("model_provider") or getattr(profile, "model_provider", "") or "custom").strip()
    table = _codex_provider_table(config, provider_id)
    if table.get("wire_api") == wire_api:
        return False
    table["wire_api"] = wire_api
    return True


def _remote_codex_current_wire_api(config: dict, profile) -> str:
    provider_id = str(config.get("model_provider") or getattr(profile, "model_provider", "") or "custom").strip()
    model_providers = config.get("model_providers")
    table = {}
    if isinstance(model_providers, dict):
        maybe_table = model_providers.get(provider_id)
        if isinstance(maybe_table, dict):
            table = maybe_table

    wire_api = str(table.get("wire_api") or "").strip().lower()
    if wire_api in CODEX_WIRE_API_VALUES:
        return wire_api

    return ProviderRegistry.get_codex_wire_api(provider_id, custom_name=table.get("name"))


def _remote_codex_env_key(config: dict) -> str:
    provider_id = str(config.get("model_provider") or "openai").strip() or "openai"
    model_providers = config.get("model_providers")
    custom = {}
    if isinstance(model_providers, dict):
        maybe_custom = model_providers.get(provider_id)
        if isinstance(maybe_custom, dict):
            custom = maybe_custom
    env_key = str(custom.get("env_key") or "").strip()
    if env_key:
        return env_key
    return ProviderRegistry.get_codex_env_key(provider_id, custom_name=custom.get("name"))


def _remote_codex_explicit_env_key(config: dict) -> str:
    provider_id = str(config.get("model_provider") or "openai").strip() or "openai"
    model_providers = config.get("model_providers")
    if not isinstance(model_providers, dict):
        return ""
    custom = model_providers.get(provider_id)
    if not isinstance(custom, dict):
        return ""
    return str(custom.get("env_key") or "").strip()


def _remote_codex_requires_openai_auth(config: dict) -> bool:
    provider_id = str(config.get("model_provider") or "openai").strip() or "openai"
    model_providers = config.get("model_providers")
    if not isinstance(model_providers, dict):
        return False
    custom = model_providers.get(provider_id)
    return bool(custom.get("requires_openai_auth", False)) if isinstance(custom, dict) else False


def _remote_text_env_value(
    content: str | None,
    env_key: str,
    *,
    allow_openai_fallback: bool = False,
) -> str:
    if not content:
        return ""
    try:
        from core import codex_env

        values = codex_env.parse_codex_env_text(content)
    except Exception:
        values = {}
    value = values.get(env_key)
    if not value and allow_openai_fallback:
        value = values.get("OPENAI_API_KEY")
    return str(value or "").strip()


def _remote_codex_api_key_from_sources(client, ssh_profile, config: dict, auth: dict) -> tuple[str, str]:
    env_key = _remote_codex_env_key(config)
    explicit_env_key = _remote_codex_explicit_env_key(config)
    auth_openai_key = str(auth.get("OPENAI_API_KEY") or "").strip() if isinstance(auth, dict) else ""
    if isinstance(auth, dict):
        key = str(auth.get(env_key) or "").strip()
        if key:
            return key, env_key
    if not explicit_env_key and auth_openai_key:
        return auth_openai_key, "OPENAI_API_KEY"

    try:
        key = _remote_text_env_value(
            remote_config.read_remote_codex_env(client, ssh_profile),
            env_key,
            allow_openai_fallback=not explicit_env_key,
        )
        if key:
            return key, env_key
    except Exception as e:
        logger.debug("Failed to read remote Codex .env for import: %s", e)

    try:
        from core import persistent_env

        home = remote_config._remote_home(client)
        env_path = posixpath.join(home, persistent_env.REMOTE_ENV_FILENAME)
        key = _remote_text_env_value(
            ssh_manager.read_remote_file(client, env_path),
            env_key,
            allow_openai_fallback=not explicit_env_key,
        )
        if key:
            return key, env_key
    except Exception as e:
        logger.debug("Failed to read remote shell env file for Codex import: %s", e)

    if auth_openai_key and not explicit_env_key:
        return auth_openai_key, "OPENAI_API_KEY"
    return "", env_key


def _format_remote_wire_summary(summaries: list[dict]) -> str:
    parts = []
    for item in summaries:
        wire_api = item.get("wire_api", "?")
        successes = item.get("successes", 0)
        repeat_count = item.get("repeat_count", 0)
        avg_ms = item.get("avg_ms")
        avg_text = f"{avg_ms}ms" if avg_ms is not None else "-"
        parts.append(f"{wire_api} {successes}/{repeat_count} avg {avg_text}")
    return "; ".join(parts)


def _remote_benchmark_codex_wire_api(client, profile, config: dict, api_key: str) -> RemoteWireBenchmarkResult:
    base_url = _remote_codex_base_url(config, profile)
    model = _remote_codex_model(config, profile)
    if not base_url:
        return RemoteWireBenchmarkResult(False, error="缺少 Codex Base URL")
    if not model:
        return RemoteWireBenchmarkResult(False, error="缺少 Codex 模型")

    try:
        remote_home = remote_config._remote_home(client)
    except Exception as error:
        return RemoteWireBenchmarkResult(
            False,
            selected_model=model,
            error=f"无法解析远程受管代理路径: {str(error)[:220]}",
        )
    managed_proxy_env = posixpath.join(
        remote_home,
        ".config",
        "api-switcher",
        "ai-proxy.env",
    )
    managed_proxy_config = posixpath.join(
        remote_home,
        ".config",
        "mihomo",
        "config.yaml",
    )
    try:
        managed_config = ssh_manager.read_remote_file(client, managed_proxy_config)
    except Exception as error:
        return RemoteWireBenchmarkResult(
            False,
            selected_model=model,
            error=f"无法验证远程受管代理配置，已禁止携带 API Key 自测: {str(error)[:180]}",
        )

    if managed_config is None:
        proxy_validation = "absent"
    else:
        config_fingerprint = hashlib.sha256(managed_config.encode("utf-8")).hexdigest()
        config_lines = {line.strip() for line in managed_config.splitlines()}
        strict_marker_present = remote_proxy.AI_PROXY_STRICT_PRIVACY_MARKER in config_lines
        if strict_marker_present:
            if not remote_proxy._managed_config_strict_privacy_enabled(managed_config):
                return RemoteWireBenchmarkResult(
                    False,
                    selected_model=model,
                    error=(
                        "远程受管代理严格隐私配置未通过 fail-closed/DNS/IPv6 "
                        "完整契约校验，已禁止携带 API Key 自测"
                    ),
                )
            proxy_validation = f"strict-sha256:{config_fingerprint}"
        else:
            proxy_validation = f"compat-sha256:{config_fingerprint}"

    # Build the credential-bearing payload only after the routing posture has
    # been read and, for strict mode, validated against the complete contract.
    payload = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout": 10,
        "repeat_count": 3,
        "wire_apis": ["responses"],
    }
    command = (
        'PYTHON_BIN="$(command -v python3 || command -v python || true)"; '
        '[ -n "$PYTHON_BIN" ] || exit 127; '
        f'"$PYTHON_BIN" -c {shlex.quote(_REMOTE_CODEX_WIRE_BENCHMARK_SCRIPT)} '
        f'{shlex.quote(managed_proxy_env)} {shlex.quote(managed_proxy_config)} '
        f'{shlex.quote(proxy_validation)}'
    )

    try:
        status, stdout, stderr = ssh_manager.execute_command_with_status(
            client,
            command,
            timeout=140,
            input_data=json.dumps(payload),
            log_command=False,
        )
        if status != 0:
            return RemoteWireBenchmarkResult(False, selected_model=model, error=(stderr or stdout or f"exit {status}")[:300])

        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            return RemoteWireBenchmarkResult(False, selected_model=model, error="远端 wire_api 自测没有输出")
        data = json.loads(lines[-1])
        if not isinstance(data, dict):
            return RemoteWireBenchmarkResult(False, selected_model=model, error="远端 wire_api 自测输出格式无效")
        summaries = data.get("summaries") if isinstance(data.get("summaries"), list) else []
        return RemoteWireBenchmarkResult(
            success=bool(data.get("success")),
            recommended_wire_api=data.get("recommended_wire_api"),
            selected_model=str(data.get("selected_model") or model),
            summary=_format_remote_wire_summary(summaries),
            error="" if data.get("success") else str(data.get("error") or "所有 wire_api 远端测试均失败"),
        )
    except Exception as e:
        logger.warning("Remote Codex wire_api benchmark skipped: %s", e)
        return RemoteWireBenchmarkResult(False, selected_model=model, error=str(e)[:300])


def _remote_codex_login_status(
    client,
    ssh_profile=None,
    *,
    clear_api_env: bool = False,
) -> tuple[bool | None, str]:
    try:
        codex_home = remote_config._expand_remote_path(
            client,
            remote_config._remote_dir(ssh_profile, "codex", client),
        )
    except Exception as error:
        return None, f"无法解析远程 CODEX_HOME: {error}"[:300]
    clean_env = (
        f"unset {' '.join(REMOTE_CODEX_API_ENV_NAMES)}; "
        if clear_api_env
        else ""
    )
    command = (
        f"{clean_env}export CODEX_HOME={shlex.quote(codex_home)}; "
        'CODEX_BIN="$(command -v codex || true)"; '
        '[ -n "$CODEX_BIN" ] || CODEX_BIN="$(find "$HOME/.vscode-server/extensions" "$HOME/.cursor-server/extensions" '
        '-path "*/bin/*/codex" -type f 2>/dev/null | sort | tail -n 1)"; '
        '[ -n "$CODEX_BIN" ] || { echo "codex CLI not found"; exit 127; }; '
        '"$CODEX_BIN" login status 2>&1'
    )
    try:
        status, stdout, stderr = ssh_manager.execute_command_with_status(
            client,
            command,
            timeout=30,
            log_command=False,
        )
    except Exception as e:
        return None, str(e)[:300]

    output = _redact_output(stdout or stderr)
    lowered = output.lower()
    if status == 127 or "codex cli not found" in lowered:
        return None, output[:300] or "codex CLI not found"
    ok = status == 0 and "error " not in lowered and "invalid configuration" not in lowered
    return ok, output[:300]


def sync_claude_to_server(ssh_name: str, claude_name: str) -> str:
    """Sync Claude profile to remote server. Returns status message."""
    claude_profile = _find_profile(profile_manager.list_switchable_claude_profiles(), claude_name, "Claude API Profile")
    if not profile_manager.is_third_party_claude_profile(claude_profile):
        raise ValueError("只能同步第三方 Claude API Profile")
    provider = ProviderRegistry.get_provider(getattr(claude_profile, "provider", ""))
    if provider is not None and not provider.claude_supported:
        raise ValueError(f"Provider '{provider.display_name}' 不支持 Claude Code")
    claude_base_url = validate_api_base_url(
        getattr(claude_profile, "base_url", ""),
        default=ProviderRegistry.get_claude_base_url(
            getattr(claude_profile, "provider", "")
        ),
    )
    claude_profile = deepcopy(claude_profile)
    claude_profile.base_url = claude_base_url
    if not (security.get_secret(claude_profile.auth_token_ref) or security.get_secret(getattr(claude_profile, "primary_api_key_ref", None))):
        raise ValueError("Claude API Profile 需要 API Key 或 Auth Token")

    ssh_profile, client = _connect_ssh(ssh_name)

    settings_path = remote_config._remote_path("claude_settings", ssh_profile, client)
    config_path = remote_config._remote_path("claude_config", ssh_profile, client)
    is_root = _is_root_ssh_user(ssh_profile, client)
    vscode_paths = _remote_vscode_paths(client) if is_root else ()
    snapshot = _remote_file_snapshot(client, (settings_path, config_path, *vscode_paths))
    _validate_existing_remote_json_texts(client, snapshot, vscode_paths)
    settings = _strict_remote_read(remote_config.read_remote_claude_settings, client, ssh_profile) or {}
    current_config = _strict_remote_read(remote_config.read_remote_claude_config, client, ssh_profile) or {}
    settings = parser.apply_claude_profile(settings, claude_profile)
    settings, root_adjusted = _make_claude_settings_root_safe(settings, ssh_profile, client)
    config = parser.apply_claude_config(current_config, claude_profile)
    vscode_root_adjusted = False

    def write_and_verify():
        nonlocal vscode_root_adjusted
        remote_config.write_remote_claude_settings(client, settings, ssh_profile)
        remote_config.write_remote_claude_config(client, config, ssh_profile)
        _validate_remote_claude_api(client, ssh_profile, settings, config)
        vscode_root_adjusted = _sync_remote_vscode_root_safety(client, ssh_profile)

    _run_remote_transaction(client, snapshot, "远程 Claude API 同步", write_and_verify)

    logger.info(f"Synced Claude API profile '{claude_name}' to {ssh_profile.host}")
    message = f"已同步 Claude API '{claude_name}' 到 {ssh_profile.host}"
    return f"{message} | {ROOT_BYPASS_ADJUSTED_MESSAGE}" if root_adjusted or (is_root and vscode_root_adjusted) else message


def sync_claude_account_to_server(ssh_name: str, account_name: str) -> str:
    """Sync a saved Claude official account snapshot to the remote server."""
    account = _find_profile(profile_manager.list_claude_account_profiles(), account_name, "Claude 官方账号")
    credentials = profile_manager.load_claude_account_credentials(account)

    ssh_profile, client = _connect_ssh(ssh_name)

    credentials_path = remote_config._remote_path("claude_credentials", ssh_profile, client)
    settings_path = remote_config._remote_path("claude_settings", ssh_profile, client)
    config_path = remote_config._remote_path("claude_config", ssh_profile, client)
    persistent_env_path = _remote_persistent_env_path(client)
    is_root = _is_root_ssh_user(ssh_profile, client)
    vscode_paths = _remote_vscode_paths(client)
    snapshot = _remote_file_snapshot(
        client,
        (credentials_path, settings_path, config_path, persistent_env_path, *vscode_paths),
    )
    _validate_existing_remote_json_texts(client, snapshot, vscode_paths)
    # Strictly parse every existing destination before the first mutation.
    _strict_remote_read(remote_config.read_remote_claude_credentials, client, ssh_profile)
    settings = _strict_remote_read(remote_config.read_remote_claude_settings, client, ssh_profile) or {}
    config = _strict_remote_read(remote_config.read_remote_claude_config, client, ssh_profile) or {}
    settings = parser.clear_claude_api_overrides(settings)
    settings, root_adjusted = _make_claude_settings_root_safe(settings, ssh_profile, client)
    config = parser.clear_claude_config_auth(config)
    from core import persistent_env

    persistent_before = snapshot[persistent_env_path]
    persistent_expected = (
        None
        if persistent_before is None
        else persistent_env._remove_env_exports(
            persistent_before,
            REMOTE_CLAUDE_API_ENV_NAMES,
        )
    )
    vscode_root_adjusted = False

    def write_and_verify():
        nonlocal vscode_root_adjusted
        remote_config.write_remote_claude_credentials(client, credentials, ssh_profile)
        persistent_env.delete_remote_user_env(client, REMOTE_CLAUDE_API_ENV_NAMES)
        remote_config.write_remote_claude_settings(client, settings, ssh_profile)
        remote_config.write_remote_claude_config(client, config, ssh_profile)
        _validate_remote_claude_account(client, ssh_profile, credentials, settings, config)
        if remote_config.read_remote_text(client, persistent_env_path) != persistent_expected:
            raise RuntimeError("远程 Claude 持久环境变量清理后回读不一致")
        vscode_root_adjusted = _sync_remote_vscode_official_account(
            client,
            ssh_profile,
            str(settings.get("model") or ""),
        )

    _run_remote_transaction(client, snapshot, "远程 Claude 账号同步", write_and_verify)

    logger.info(f"Synced Claude account '{account_name}' to {ssh_profile.host}")
    message = f"已同步 Claude 账号 '{account_name}' 到 {ssh_profile.host}"
    return f"{message} | {ROOT_BYPASS_ADJUSTED_MESSAGE}" if root_adjusted or (is_root and vscode_root_adjusted) else message


def sync_codex_to_server(ssh_name: str, codex_name: str, wire_api_mode: str | None = CODEX_WIRE_API_AUTO) -> str:
    """Sync Codex profile to remote server. Returns status message."""
    wire_api_mode = normalize_codex_wire_api_mode(wire_api_mode)
    codex_profile = _find_profile(profile_manager.list_switchable_codex_profiles(), codex_name, "Codex API Profile")
    if not profile_manager.is_third_party_codex_profile(codex_profile):
        raise ValueError("只能同步第三方 Codex API Profile")
    provider = ProviderRegistry.get_provider(getattr(codex_profile, "model_provider", ""))
    if provider is not None and not provider.codex_supported:
        raise ValueError(f"Provider '{provider.display_name}' 不支持 Codex")
    codex_profile.validate_runtime_options()
    codex_base_url = validate_api_base_url(
        getattr(codex_profile, "custom_base_url", ""),
        default=ProviderRegistry.get_codex_base_url(
            getattr(codex_profile, "model_provider", "")
        ),
    )
    codex_profile = deepcopy(codex_profile)
    codex_profile.custom_base_url = codex_base_url
    uses_openai_auth = bool(getattr(codex_profile, "custom_requires_openai_auth", False))
    # Validate untrusted env_key metadata before opening SSH or writing any file.
    target_env_key = codex_profile.validated_env_key()
    api_key = "" if uses_openai_auth else _codex_profile_api_key(codex_profile)
    if not uses_openai_auth and not api_key:
        raise ValueError("Codex API Profile 需要 API Key")

    ssh_profile, client = _connect_ssh(ssh_name)

    config_path = remote_config._remote_path("codex_config", ssh_profile, client)
    auth_path = remote_config._remote_path("codex_auth", ssh_profile, client)
    codex_env_path = remote_config._remote_path("codex_env", ssh_profile, client)
    persistent_env_path = _remote_persistent_env_path(client)
    snapshot = _remote_file_snapshot(
        client,
        (config_path, auth_path, codex_env_path, persistent_env_path, *_remote_shell_env_paths(client)),
    )
    config = _strict_remote_read(remote_config.read_remote_codex_config, client, ssh_profile) or {}
    auth = _strict_remote_read(remote_config.read_remote_codex_auth, client, ssh_profile) or {}
    config = toml_parser.apply_codex_profile(config, codex_profile)
    if wire_api_mode in CODEX_WIRE_API_VALUES:
        _set_remote_codex_wire_api(config, codex_profile, wire_api_mode)
    auth = auth_parser.apply_codex_apikey(auth, codex_profile)
    env_keys = [] if target_env_key is None else [target_env_key]
    benchmark = None

    def write_and_verify():
        nonlocal benchmark
        remote_config.write_remote_codex_config(client, config, ssh_profile)
        remote_config.write_remote_codex_auth(client, auth, ssh_profile)
        persisted_env_keys = _persist_remote_codex_env(client, codex_profile, api_key, ssh_profile)
        if persisted_env_keys != env_keys:
            raise RuntimeError("远程 Codex 环境变量写入目标不一致")
        _validate_remote_codex_api(client, ssh_profile, config, auth)
        _validate_remote_codex_env(client, ssh_profile, persistent_env_path, env_keys, api_key)
        if wire_api_mode == CODEX_WIRE_API_AUTO and api_key:
            benchmark = _remote_benchmark_codex_wire_api(client, codex_profile, config, api_key)
            if benchmark.success and benchmark.recommended_wire_api:
                if _set_remote_codex_wire_api(config, codex_profile, benchmark.recommended_wire_api):
                    remote_config.write_remote_codex_config(client, config, ssh_profile)
                    _validate_remote_codex_api(client, ssh_profile, config, auth)

    _run_remote_transaction(client, snapshot, "远程 Codex API 同步", write_and_verify)

    logger.info(f"Synced Codex API profile '{codex_name}' to {ssh_profile.host}")
    if uses_openai_auth:
        message = f"已同步 Codex API '{codex_name}' 到 {ssh_profile.host} | 使用 OpenAI 登录/API Key 认证"
    else:
        message = f"已同步 Codex API '{codex_name}' 到 {ssh_profile.host} | 已写入远端环境变量 {', '.join(env_keys)}"
    current_wire_api = _remote_codex_current_wire_api(config, codex_profile)
    validation_ok, validation_output = _remote_codex_login_status(client, ssh_profile)
    validation_detail = ""
    if validation_output:
        if validation_ok is None:
            status_text = "未验证"
        else:
            status_text = "通过" if validation_ok else "失败"
        validation_detail = f" | 远端 Codex 验证{status_text}: {validation_output}"
    if benchmark and benchmark.success and benchmark.recommended_wire_api:
        detail = f" | 远端自测已选择 wire_api={benchmark.recommended_wire_api}"
        if benchmark.selected_model:
            detail += f"，模型={benchmark.selected_model}"
        if benchmark.summary:
            detail += f"（{benchmark.summary}）"
        return message + detail + validation_detail
    if benchmark and benchmark.error:
        return message + f" | 远端 wire_api 自测跳过: {benchmark.error}；当前使用 wire_api={current_wire_api}" + validation_detail
    if wire_api_mode in CODEX_WIRE_API_VALUES:
        return message + f" | 已手动选择 wire_api={current_wire_api}" + validation_detail
    return message + f" | 使用本地配置 wire_api={current_wire_api}" + validation_detail


def sync_codex_account_to_server(ssh_name: str, account_name: str) -> str:
    """Sync a saved Codex official account snapshot to the remote server."""
    account = _find_profile(profile_manager.list_codex_account_profiles(), account_name, "Codex 官方账号")
    auth = profile_manager.load_codex_account_auth(account)

    ssh_profile, client = _connect_ssh(ssh_name)

    # Read and validate every existing file before the first write.  A corrupt
    # config must never leave auth.json half-switched.  Include both places
    # where this app persists provider environment variables in the snapshot.
    auth_path = remote_config._remote_path("codex_auth", ssh_profile, client)
    config_path = remote_config._remote_path("codex_config", ssh_profile, client)
    codex_env_path = remote_config._remote_path("codex_env", ssh_profile, client)
    persistent_env_path = _remote_persistent_env_path(client)
    snapshot = _remote_file_snapshot(client, (auth_path, config_path, codex_env_path, persistent_env_path))
    _strict_remote_read(remote_config.read_remote_codex_auth, client, ssh_profile)
    old_config = _strict_remote_read(remote_config.read_remote_codex_config, client, ssh_profile)
    old_codex_env = snapshot[codex_env_path]
    from core import codex_env, persistent_env
    config = old_config or {}
    updated_config = toml_parser.apply_codex_official_account(deepcopy(config))
    validation_ok = None
    validation_output = ""

    def write_and_verify():
        nonlocal validation_ok, validation_output
        remote_config.write_remote_codex_auth(client, auth, ssh_profile)
        remote_config.write_remote_codex_config(client, updated_config, ssh_profile)

        env_names = _remote_codex_account_switch_env_names(config)
        persistent_env.delete_remote_user_env(client, env_names)
        if old_codex_env is not None:
            remote_config.write_remote_codex_env(
                client,
                codex_env.merge_codex_env_text(old_codex_env, deletes=env_names),
                ssh_profile,
            )

        written_auth = _strict_remote_read(remote_config.read_remote_codex_auth, client, ssh_profile)
        ok, reason = profile_manager._validate_codex_account_auth(written_auth)
        if not ok or written_auth != auth:
            raise RuntimeError(f"远程 Codex 账号写入后校验失败: {reason}")
        written_config = _strict_remote_read(remote_config.read_remote_codex_config, client, ssh_profile) or {}
        if (
            written_config.get("model_provider", "openai") != "openai"
            or written_config.get("cli_auth_credentials_store") != "file"
        ):
            raise RuntimeError("远程 Codex 官方账号配置写入后校验失败")
        _validate_remote_codex_env_removed(client, ssh_profile, persistent_env_path, env_names)

        validation_ok, validation_output = _remote_codex_login_status(
            client,
            ssh_profile,
            clear_api_env=True,
        )
        if validation_ok is False:
            detail = validation_output or "codex login status 返回失败"
            raise RuntimeError(f"远程 Codex 登录状态校验失败: {detail}")

    _run_remote_transaction(client, snapshot, "远程 Codex 账号切换", write_and_verify)

    logger.info(f"Synced Codex account '{account_name}' to {ssh_profile.host}")
    if validation_ok is None:
        detail = f" | 凭据已写入并回读校验，CLI 未可用，未执行登录状态检查: {validation_output}"
    else:
        detail = f" | {validation_output}" if validation_output else ""
    return f"已同步 Codex 账号 '{account_name}' 到 {ssh_profile.host}{detail}"


def sync_selected_to_server(
    ssh_name: str,
    target_kind: str,
    name: str,
    codex_wire_api_mode: str | None = CODEX_WIRE_API_AUTO,
) -> str:
    """Sync one explicit local API profile or official account snapshot to the remote server."""
    if target_kind == "codex_api":
        return sync_codex_to_server(ssh_name, name, codex_wire_api_mode)

    handlers = {
        "claude_api": sync_claude_to_server,
        "claude_account": sync_claude_account_to_server,
        "codex_account": sync_codex_account_to_server,
    }
    handler = handlers.get(target_kind)
    if not handler:
        raise ValueError(f"不支持的同步类型: {target_kind}")
    return handler(ssh_name, name)


def sync_all_to_server(ssh_name: str, codex_wire_api_mode: str | None = CODEX_WIRE_API_AUTO) -> str:
    """Sync currently active local Claude + Codex target to remote server."""
    results = []
    failures = []

    claude_api = {p.name for p in profile_manager.list_switchable_claude_profiles()}
    claude_accounts = {p.name for p in profile_manager.list_claude_account_profiles()}
    active_claude_api = profile_manager.get_current_claude_name() or profile_manager.get_active_claude_name()
    active_claude_account = profile_manager.get_current_claude_account_name() or profile_manager.get_active_claude_account_name()
    if active_claude_api in claude_api:
        try:
            results.append(sync_claude_to_server(ssh_name, active_claude_api))
        except Exception as e:
            failures.append(f"Claude: {e}")
    elif active_claude_account in claude_accounts:
        try:
            results.append(sync_claude_account_to_server(ssh_name, active_claude_account))
        except Exception as e:
            failures.append(f"Claude 账号: {e}")

    codex_api = {p.name for p in profile_manager.list_switchable_codex_profiles()}
    codex_accounts = {p.name for p in profile_manager.list_codex_account_profiles()}
    active_codex_api = profile_manager.get_current_codex_name() or profile_manager.get_active_codex_name()
    active_codex_account = profile_manager.get_current_codex_account_name() or profile_manager.get_active_codex_account_name()
    if active_codex_api in codex_api:
        try:
            results.append(sync_codex_to_server(ssh_name, active_codex_api, codex_wire_api_mode))
        except Exception as e:
            failures.append(f"Codex: {e}")
    elif active_codex_account in codex_accounts:
        try:
            results.append(sync_codex_account_to_server(ssh_name, active_codex_account))
        except Exception as e:
            failures.append(f"Codex 账号: {e}")

    if results and failures:
        return " | ".join(results) + " | 部分失败: " + "；".join(failures)
    if results:
        return " | ".join(results)
    if failures:
        raise RuntimeError("；".join(failures))
    return "没有当前生效的 API 或账号可同步"


def _clear_remote_claude_api_info(client, ssh_profile) -> str:
    from core import persistent_env

    settings_path = remote_config._remote_path("claude_settings", ssh_profile, client)
    config_path = remote_config._remote_path("claude_config", ssh_profile, client)
    persistent_env_path = _remote_persistent_env_path(client)
    snapshot = _remote_file_snapshot(
        client,
        (settings_path, config_path, persistent_env_path),
    )
    settings = _strict_remote_read(remote_config.read_remote_claude_settings, client, ssh_profile)
    config = _strict_remote_read(remote_config.read_remote_claude_config, client, ssh_profile)
    cleaned_settings = parser.clear_claude_api_overrides(settings) if settings is not None else None
    cleaned_config = parser.clear_claude_config_auth(config) if config is not None else None
    persistent_before = snapshot[persistent_env_path]
    persistent_expected = (
        None
        if persistent_before is None
        else persistent_env._remove_env_exports(
            persistent_before,
            REMOTE_CLAUDE_API_ENV_NAMES,
        )
    )
    touched = settings is not None or config is not None
    changed = (
        cleaned_settings != settings
        or cleaned_config != config
        or persistent_expected != persistent_before
    )

    def write_and_verify():
        if settings is not None and cleaned_settings != settings:
            remote_config.write_remote_claude_settings(client, cleaned_settings, ssh_profile)
        if config is not None and cleaned_config != config:
            remote_config.write_remote_claude_config(client, cleaned_config, ssh_profile)
        persistent_env.delete_remote_user_env(client, REMOTE_CLAUDE_API_ENV_NAMES)

        written_settings = _strict_remote_read(
            remote_config.read_remote_claude_settings,
            client,
            ssh_profile,
        )
        written_config = _strict_remote_read(
            remote_config.read_remote_claude_config,
            client,
            ssh_profile,
        )
        if written_settings != cleaned_settings or written_config != cleaned_config:
            raise RuntimeError("远程 Claude API 信息清理后回读不一致")
        if remote_config.read_remote_text(client, persistent_env_path) != persistent_expected:
            raise RuntimeError("远程 Claude 持久环境变量清理后回读不一致")

    _run_remote_transaction(client, snapshot, "远程 Claude API 信息清理", write_and_verify)

    if changed:
        return "Claude API 信息已清除"
    if touched:
        return "Claude 未发现可清除的 API 覆盖"
    return "Claude 配置文件不存在，已清理相关环境变量"


def _remote_codex_api_env_names(config: dict | None) -> tuple[str, ...]:
    names = set(REMOTE_CODEX_API_ENV_NAMES)
    if isinstance(config, dict):
        provider_id = str(config.get("model_provider") or "").strip()
        model_providers = config.get("model_providers")
        if provider_id and isinstance(model_providers, dict):
            provider_table = model_providers.get(provider_id)
            if isinstance(provider_table, dict):
                env_key = str(provider_table.get("env_key") or "").strip()
                if env_key:
                    try:
                        names.add(ProviderRegistry.validate_codex_env_key(env_key))
                    except ValueError as error:
                        logger.warning("Skipping unsafe remote Codex env cleanup target %r: %s", env_key, error)
    return tuple(sorted(names))


def _remote_codex_account_switch_env_names(config: dict | None) -> tuple[str, ...]:
    """Return only API overrides that can affect the active Codex provider.

    Switching to an official account must not remove unrelated provider keys
    that happen to share the app-managed remote environment file.
    """
    names = {"OPENAI_API_KEY", "OPENAI_BASE_URL"}
    if isinstance(config, dict):
        provider_id = str(config.get("model_provider") or "").strip()
        model_providers = config.get("model_providers")
        if provider_id and isinstance(model_providers, dict):
            provider_table = model_providers.get(provider_id)
            if isinstance(provider_table, dict):
                env_key = str(provider_table.get("env_key") or "").strip()
                if env_key:
                    try:
                        names.add(ProviderRegistry.validate_codex_env_key(env_key))
                    except ValueError as error:
                        logger.warning("Skipping unsafe remote Codex account env cleanup target %r: %s", env_key, error)
    return tuple(sorted(names))


def _clear_remote_codex_api_info(client, ssh_profile) -> str:
    from core import codex_env, persistent_env

    config_path = remote_config._remote_path("codex_config", ssh_profile, client)
    auth_path = remote_config._remote_path("codex_auth", ssh_profile, client)
    codex_env_path = remote_config._remote_path("codex_env", ssh_profile, client)
    persistent_env_path = _remote_persistent_env_path(client)
    snapshot = _remote_file_snapshot(
        client,
        (config_path, auth_path, codex_env_path, persistent_env_path),
    )
    config = _strict_remote_read(remote_config.read_remote_codex_config, client, ssh_profile)
    auth = _strict_remote_read(remote_config.read_remote_codex_auth, client, ssh_profile)
    cleaned_config = toml_parser.clear_codex_api_overrides(config) if config is not None else None
    cleaned_auth = auth_parser.clear_codex_api_auth(auth) if auth is not None else None
    env_names = _remote_codex_api_env_names(config)
    codex_env_before = snapshot[codex_env_path]
    codex_env_expected = (
        None
        if codex_env_before is None
        else codex_env.merge_codex_env_text(codex_env_before, deletes=env_names)
    )
    persistent_before = snapshot[persistent_env_path]
    persistent_expected = (
        None
        if persistent_before is None
        else persistent_env._remove_env_exports(persistent_before, env_names)
    )
    touched = config is not None or auth is not None
    changed = (
        cleaned_config != config
        or cleaned_auth != auth
        or codex_env_expected != codex_env_before
        or persistent_expected != persistent_before
    )

    def write_and_verify():
        if config is not None and cleaned_config != config:
            remote_config.write_remote_codex_config(client, cleaned_config, ssh_profile)
        if auth is not None and cleaned_auth != auth:
            remote_config.write_remote_codex_auth(client, cleaned_auth, ssh_profile)
        persistent_env.delete_remote_user_env(client, env_names)
        if codex_env_before is not None and codex_env_expected != codex_env_before:
            remote_config.write_remote_codex_env(
                client,
                codex_env_expected,
                ssh_profile,
            )

        written_config = _strict_remote_read(
            remote_config.read_remote_codex_config,
            client,
            ssh_profile,
        )
        written_auth = _strict_remote_read(
            remote_config.read_remote_codex_auth,
            client,
            ssh_profile,
        )
        if written_config != cleaned_config or written_auth != cleaned_auth:
            raise RuntimeError("远程 Codex API 信息清理后回读不一致")
        if remote_config.read_remote_text(client, codex_env_path) != codex_env_expected:
            raise RuntimeError("远程 Codex .env 清理后回读不一致")
        if remote_config.read_remote_text(client, persistent_env_path) != persistent_expected:
            raise RuntimeError("远程 Codex 持久环境变量清理后回读不一致")

    _run_remote_transaction(client, snapshot, "远程 Codex API 信息清理", write_and_verify)

    if changed:
        return "Codex API 信息已清除"
    if touched:
        return "Codex 未发现可清除的 API 覆盖"
    return "Codex 配置文件不存在，已清理相关环境变量"


def clear_remote_api_info(ssh_name: str, target: str = "all") -> str:
    """Clear current Claude/Codex API runtime information from an SSH server."""
    target = str(target or "all").strip().lower()
    if target not in REMOTE_API_CLEAR_TARGETS:
        raise ValueError(f"不支持的远端 API 清理目标: {target}")

    ssh_profile, client = _connect_ssh(ssh_name)
    results = []
    if target in {"claude", "all"}:
        results.append(_clear_remote_claude_api_info(client, ssh_profile))
    if target in {"codex", "all"}:
        results.append(_clear_remote_codex_api_info(client, ssh_profile))

    logger.info("Cleared remote API info on %s for target=%s", ssh_profile.host, target)
    return f"已清理 {ssh_profile.host}： " + "；".join(results)


def _inspect_remote_claude_config(client, ssh_profile) -> RemoteConfigCandidate:
    paths = (
        remote_config._remote_path("claude_settings", ssh_profile, client),
        remote_config._remote_path("claude_config", ssh_profile, client),
    )
    try:
        settings = _strict_remote_read(remote_config.read_remote_claude_settings, client, ssh_profile)
        config = _strict_remote_read(remote_config.read_remote_claude_config, client, ssh_profile) or {}
    except Exception as e:
        return RemoteConfigCandidate(
            kind="claude",
            label="Claude API",
            product="claude",
            reason=f"读取失败: {e}",
            paths=paths,
        )
    if not settings and not config:
        return RemoteConfigCandidate(
            kind="claude",
            label="Claude API",
            product="claude",
            reason="服务器上未找到 Claude 配置",
            paths=paths,
        )
    if not isinstance(settings, dict):
        settings = {}
    env = settings.get("env", {})
    if not isinstance(env, dict):
        env = {}
    token_value = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or config.get("primaryApiKey", "")).strip()
    provider = profile_manager.detect_claude_provider(settings)
    is_official = provider == "anthropic"
    reason = "可导入为第三方 Claude API Profile"
    if is_official:
        reason = "官方 Anthropic 配置，当前只导入第三方 API Profile"
    elif not token_value:
        reason = "未找到 Anthropic API Key/Auth Token"
    return RemoteConfigCandidate(
        kind="claude",
        label="Claude API",
        product="claude",
        provider=provider,
        provider_label=_provider_display_name(provider),
        model=str(settings.get("model") or env.get("ANTHROPIC_MODEL") or ""),
        base_url=str(env.get("ANTHROPIC_BASE_URL") or ""),
        has_api_key=bool(token_value),
        importable=bool(token_value) and not is_official,
        reason=reason,
        paths=paths,
    )


def _inspect_remote_claude_account(client, ssh_profile) -> RemoteConfigCandidate:
    paths = (remote_config._remote_path("claude_credentials", ssh_profile, client),)
    try:
        credentials = _strict_remote_read(remote_config.read_remote_claude_credentials, client, ssh_profile)
    except Exception as e:
        return RemoteConfigCandidate(
            kind="claude_account",
            label="Claude 账号",
            category="account",
            product="claude",
            reason=f"读取失败: {e}",
            paths=paths,
        )

    ok, reason = profile_manager._validate_claude_account_credentials(credentials)
    identity = ""
    preferred_name = ""
    if isinstance(credentials, dict) and credentials:
        identity = profile_manager._claude_account_identity_from_credentials(credentials)
        preferred_name = profile_manager._claude_account_preferred_name(credentials)
    return RemoteConfigCandidate(
        kind="claude_account",
        label="Claude 账号",
        category="account",
        product="claude",
        provider="official",
        provider_label=preferred_name or identity or "官方账号",
        has_api_key=ok,
        importable=ok,
        reason="可导入为 Claude 官方账号快照" if ok else reason,
        paths=paths,
    )


def _inspect_remote_codex_config(client, ssh_profile) -> RemoteConfigCandidate:
    paths = (
        remote_config._remote_path("codex_config", ssh_profile, client),
        remote_config._remote_path("codex_auth", ssh_profile, client),
    )
    try:
        config = _strict_remote_read(remote_config.read_remote_codex_config, client, ssh_profile)
        auth = _strict_remote_read(remote_config.read_remote_codex_auth, client, ssh_profile)
    except Exception as e:
        return RemoteConfigCandidate(
            kind="codex",
            label="Codex API",
            product="codex",
            reason=f"读取失败: {e}",
            paths=paths,
        )
    if not config and not auth:
        return RemoteConfigCandidate(
            kind="codex",
            label="Codex API",
            product="codex",
            reason="服务器上未找到 Codex 配置",
            paths=paths,
        )
    if not isinstance(config, dict):
        config = {}
    if not isinstance(auth, dict):
        auth = {}
    provider_id = str(config.get("model_provider") or "openai")
    requires_openai_auth = _remote_codex_requires_openai_auth(config)
    api_key, env_key = _remote_codex_api_key_from_sources(client, ssh_profile, config, auth)
    has_key = bool(api_key)
    reason = "可导入为第三方 Codex API Profile"
    if provider_id == "openai":
        reason = "官方 OpenAI 配置，当前只导入第三方 API Profile"
    elif requires_openai_auth:
        reason = "使用 OpenAI 认证，可导入第三方 Provider 设置"
    elif not has_key:
        reason = f"未找到 {env_key}"
    custom_name = ""
    model_providers = config.get("model_providers")
    if isinstance(model_providers, dict):
        custom = model_providers.get(provider_id)
        if isinstance(custom, dict):
            custom_name = str(custom.get("name") or "")
    return RemoteConfigCandidate(
        kind="codex",
        label="Codex API",
        product="codex",
        provider=provider_id,
        provider_label=custom_name or _provider_display_name(provider_id),
        model=str(config.get("model") or ""),
        base_url=str((model_providers.get(provider_id, {}) if isinstance(model_providers, dict) else {}).get("base_url") or ""),
        has_api_key=has_key,
        importable=(has_key or requires_openai_auth) and provider_id != "openai",
        reason=reason,
        paths=paths,
    )


def _inspect_remote_codex_account(client, ssh_profile) -> RemoteConfigCandidate:
    paths = (
        remote_config._remote_path("codex_auth", ssh_profile, client),
        remote_config._remote_path("codex_config", ssh_profile, client),
    )
    try:
        config = remote_config.read_remote_codex_config(client, ssh_profile, strict=True) or {}
        store = str(config.get("cli_auth_credentials_store") or "auto").strip().lower()
        auth = remote_config.read_remote_codex_auth(client, ssh_profile, strict=True) if store == "file" else None
    except Exception as e:
        return RemoteConfigCandidate(
            kind="codex_account",
            label="Codex 账号",
            category="account",
            product="codex",
            reason=f"读取失败: {e}",
            paths=paths,
        )

    ok, reason = profile_manager._validate_codex_account_auth(auth)
    if store != "file":
        _login_ok, login_status = _remote_codex_login_status(client, ssh_profile)
        reason = (
            f"远程 Codex 配置使用 {store} 凭据库，不会读取可能过期的 auth.json；"
            "请在远程明确设置 cli_auth_credentials_store=\"file\" 后重新登录"
        )
        if login_status:
            reason += f"；Codex 登录状态（仅作提示）: {login_status}"
    override_active = profile_manager._codex_account_override_active(config, auth or {}) if isinstance(auth, dict) else True
    identity = ""
    preferred_name = ""
    if isinstance(auth, dict) and auth:
        identity = profile_manager._codex_account_identity_from_auth(auth)
        preferred_name = profile_manager._codex_account_preferred_name(auth)
    if ok and override_active:
        reason = "检测到第三方 API 覆盖；可导入账号快照，但远端当前优先使用 API"
    return RemoteConfigCandidate(
        kind="codex_account",
        label="Codex 账号",
        category="account",
        product="codex",
        provider="official",
        provider_label=preferred_name or identity or "官方账号",
        has_api_key=ok,
        importable=ok,
        reason=reason if ok and override_active else "可导入为 Codex 官方账号快照" if ok else reason,
        paths=paths,
    )


def inspect_remote_configs(ssh_name: str) -> list[RemoteConfigCandidate]:
    """Inspect remote Claude/Codex API config before importing anything locally."""
    ssh_profile, client = _connect_ssh(ssh_name)
    return [
        _inspect_remote_claude_config(client, ssh_profile),
        _inspect_remote_claude_account(client, ssh_profile),
        _inspect_remote_codex_config(client, ssh_profile),
        _inspect_remote_codex_account(client, ssh_profile),
    ]


def pull_remote_config_from_server(ssh_name: str, kind: str) -> str:
    """Pull one inspected remote config kind from an SSH server."""
    kind = str(kind or "").strip().lower()
    if kind in {"claude", "claude_api"}:
        return pull_claude_from_server(ssh_name)
    if kind == "claude_account":
        return pull_claude_account_from_server(ssh_name)
    if kind in {"codex", "codex_api"}:
        return pull_codex_from_server(ssh_name)
    if kind == "codex_account":
        return pull_codex_account_from_server(ssh_name)
    raise ValueError(f"不支持的远端配置类型: {kind}")


def pull_claude_account_from_server(ssh_name: str) -> str:
    """Pull Claude official account credentials from server and save as an account profile."""
    ssh_profile, client = _connect_ssh(ssh_name)
    credentials = _strict_remote_read(remote_config.read_remote_claude_credentials, client, ssh_profile)
    ok, reason = profile_manager._validate_claude_account_credentials(credentials)
    if not ok:
        return f"远程 Claude 账号不可导入: {reason}"

    from models.profile import ClaudeAccountProfile

    identity = profile_manager._claude_account_identity_from_credentials(credentials)
    preferred_name = profile_manager._claude_account_preferred_name(credentials)
    name = profile_manager._pick_claude_account_import_name(identity, preferred_name, credentials)
    ref = f"claude-account:{name}:credentials"
    profile = ClaudeAccountProfile(
        name=name,
        credentials_ref=ref,
        identity=identity,
        created_at=profile_manager._now_iso(),
    )
    profile_manager.save_claude_account_profile_with_credentials(profile, credentials)
    return f"已从 {ssh_profile.host} 拉取 Claude 账号，保存为 '{name}'"


def pull_claude_from_server(ssh_name: str) -> str:
    """Pull Claude config from server and save as a profile."""
    ssh_profile, client = _connect_ssh(ssh_name)
    settings = _strict_remote_read(remote_config.read_remote_claude_settings, client, ssh_profile)
    config = _strict_remote_read(remote_config.read_remote_claude_config, client, ssh_profile) or {}
    if not settings and not config:
        return "服务器上未找到 Claude 配置"
    if not settings:
        settings = {}

    # Create profile from remote settings
    name = f"Remote-{ssh_name}"
    env = settings.get("env", {})
    if not isinstance(env, dict):
        env = {}
    token_value = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or config.get("primaryApiKey", "")
    token_value = str(token_value or "").strip()
    provider = profile_manager.detect_claude_provider(settings)
    if provider == "anthropic":
        return "远程 Claude 配置是官方 Anthropic，已跳过；当前只导入第三方 API Profile"
    if not token_value:
        return "远程 Claude 配置没有 API Key/Auth Token，已跳过；当前只导入第三方 API Profile"

    from models.profile import ClaudeProfile
    token_ref = f"claude:{name}:auth_token"

    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        permissions = {}
    additional_directories = settings.get("additionalDirectories", [])
    if not isinstance(additional_directories, list):
        additional_directories = permissions.get("additionalDirectories", [])
    if not isinstance(additional_directories, list):
        additional_directories = []
    profile = ClaudeProfile(
        name=name,
        auth_token_ref=token_ref,
        primary_api_key_ref=None,
        auth_scheme=profile_manager._claude_auth_scheme_from_current(settings, config, provider),
        base_url=env.get("ANTHROPIC_BASE_URL", ""),
        model=settings.get("model") or env.get("ANTHROPIC_MODEL") or "",
        effort_level=env.get("CLAUDE_CODE_EFFORT_LEVEL") or settings.get("effortLevel") or "high",
        permissions_mode=permissions.get("defaultMode", "default"),
        skip_dangerous_prompt=settings.get("skipDangerousModePermissionPrompt", False),
        permissions_allow=permissions.get("allow", []),
        additional_directories=additional_directories,
        provider=provider,
    )
    profile_manager.save_claude_profile_with_secrets(
        profile,
        {token_ref: token_value},
    )

    return f"已从 {ssh_profile.host} 拉取 Claude 配置，保存为 '{name}'"


def pull_codex_account_from_server(ssh_name: str) -> str:
    """Pull Codex official account auth from server and save as an account profile."""
    ssh_profile, client = _connect_ssh(ssh_name)
    config = remote_config.read_remote_codex_config(client, ssh_profile, strict=True) or {}
    store = str(config.get("cli_auth_credentials_store") or "auto").strip().lower()
    if store != "file":
        _login_ok, login_status = _remote_codex_login_status(client, ssh_profile)
        detail = f"；Codex 登录状态（仅作提示）: {login_status}" if login_status else ""
        raise ValueError(
            f"远程 Codex 配置使用 {store} 凭据库，不会读取可能过期的 auth.json；"
            f"请在远程明确设置 cli_auth_credentials_store=\"file\" 后重新登录{detail}"
        )
    auth = remote_config.read_remote_codex_auth(client, ssh_profile, strict=True)
    ok, reason = profile_manager._validate_codex_account_auth(auth)
    if not ok:
        raise ValueError(f"远程 Codex 账号不可导入: {reason}")
    auth = profile_manager._normalize_codex_official_auth(auth)

    from models.profile import CodexAccountProfile

    identity = profile_manager._codex_account_identity_from_auth(auth)
    preferred_name = profile_manager._codex_account_preferred_name(auth)
    name = profile_manager._pick_codex_account_import_name(identity, preferred_name, auth)
    ref = f"codex-account:{name}:auth_json"
    profile = CodexAccountProfile(
        name=name,
        auth_json_ref=ref,
        identity=identity,
        created_at=profile_manager._now_iso(),
    )
    profile_manager.save_codex_account_profile_with_auth(profile, auth)
    return f"已从 {ssh_profile.host} 拉取 Codex 账号，保存为 '{name}'"


def pull_codex_from_server(ssh_name: str) -> str:
    """Pull Codex config from server and save as a profile."""
    ssh_profile, client = _connect_ssh(ssh_name)

    config = _strict_remote_read(remote_config.read_remote_codex_config, client, ssh_profile)
    auth = _strict_remote_read(remote_config.read_remote_codex_auth, client, ssh_profile)

    if not config and not auth:
        return "服务器上未找到 Codex 配置"

    name = f"Remote-{ssh_name}"
    provider_id = config.get("model_provider", "openai") if config else "openai"
    if provider_id == "openai":
        return "远程 Codex 配置是官方 OpenAI，已跳过；当前只导入第三方 API Profile"
    requires_openai_auth = _remote_codex_requires_openai_auth(config or {})
    api_key, env_key = _remote_codex_api_key_from_sources(client, ssh_profile, config or {}, auth or {})
    if not api_key and not requires_openai_auth:
        return f"远程 Codex 配置没有 API Key（{env_key}），已跳过；当前只导入第三方 API Profile"

    from models.profile import CodexProfile
    profile_kwargs = {
        "name": name,
        "model": config.get("model", "gpt-5.5") if config else "gpt-5.5",
        "model_provider": provider_id,
        "model_reasoning_effort": config.get("model_reasoning_effort", "high") if config else "high",
        "approval_policy": config.get("approval_policy", "never") if config else "never",
        "sandbox_mode": config.get("sandbox_mode", "danger-full-access") if config else "danger-full-access",
        "disable_response_storage": config.get("disable_response_storage", True) if config else True,
    }

    if config:
        provider_id = profile_kwargs["model_provider"]
        model_providers = config.get("model_providers", {})
        if not isinstance(model_providers, dict):
            model_providers = {}
        custom = model_providers.get(provider_id, {})
        if not isinstance(custom, dict):
            custom = {}
        if custom:
            profile_kwargs["custom_base_url"] = custom.get("base_url")
            profile_kwargs["custom_name"] = custom.get("name")
            profile_kwargs["custom_wire_api"] = ProviderRegistry.normalize_codex_wire_api(custom.get("wire_api")) or ""
            profile_kwargs["custom_env_key"] = custom.get("env_key")
            profile_kwargs["custom_requires_openai_auth"] = custom.get("requires_openai_auth", False)
    if api_key and env_key != "OPENAI_API_KEY":
        profile_kwargs["custom_env_key"] = env_key

    secret_updates = {}
    if api_key:
        ref = f"codex:{name}:api_key"
        profile_kwargs["api_key_ref"] = ref
        secret_updates[ref] = api_key

    profile = CodexProfile(**profile_kwargs)
    profile_manager.save_codex_profile_with_secrets(profile, secret_updates)

    return f"已从 {ssh_profile.host} 拉取 Codex 配置，保存为 '{name}'"
