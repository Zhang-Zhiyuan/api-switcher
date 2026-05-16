import json
import logging
import shlex
from dataclasses import dataclass
from core import remote_config, parser, toml_parser, auth_parser, profile_manager, security
from core.providers import ProviderRegistry
from core.ssh_manager import ssh_manager

logger = logging.getLogger(__name__)


ROOT_BYPASS_ADJUSTED_MESSAGE = (
    "已兼容 root 登录：Claude Code 禁止 root/sudo 使用 "
    "bypassPermissions（--dangerously-skip-permissions），已自动将远端权限模式改为 default。"
)


@dataclass(frozen=True)
class RemoteWireBenchmarkResult:
    success: bool
    recommended_wire_api: str | None = None
    selected_model: str = ""
    summary: str = ""
    error: str = ""


CODEX_WIRE_API_AUTO = "auto"
CODEX_WIRE_API_PROFILE = "profile"
CODEX_WIRE_API_VALUES = {"chat", "responses"}
CODEX_WIRE_API_MODES = CODEX_WIRE_API_VALUES | {CODEX_WIRE_API_AUTO, CODEX_WIRE_API_PROFILE}


_REMOTE_CODEX_WIRE_BENCHMARK_SCRIPT = r"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


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


def call(api_key, base_url, model, wire_api, timeout):
    if wire_api == "responses":
        url = openai_url(base_url, "responses")
        payload = {"model": model, "input": "Reply OK only.", "max_output_tokens": 8}
    else:
        url = openai_url(base_url, "chat/completions")
        payload = {"model": model, "messages": [{"role": "user", "content": "Reply OK only."}], "max_tokens": 8}

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    start = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(300).decode("utf-8", errors="replace")
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
            "error": type(error).__name__ + ": " + str(error)[:140],
        }


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as error:
        print(json.dumps({"success": False, "error": "invalid payload: " + str(error)[:160]}, ensure_ascii=False))
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
    wire_apis = payload.get("wire_apis") or ["chat", "responses"]

    summaries = []
    best = None
    for wire_api in wire_apis:
        wire_api = str(wire_api or "").strip().lower()
        if wire_api not in {"chat", "responses"}:
            continue
        results = [call(api_key, base_url, model, wire_api, timeout) for _ in range(repeat_count)]
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


def normalize_codex_wire_api_mode(mode: str | None) -> str:
    value = str(mode or CODEX_WIRE_API_AUTO).strip().lower()
    aliases = {
        "": CODEX_WIRE_API_AUTO,
        "default": CODEX_WIRE_API_AUTO,
        "remote_auto": CODEX_WIRE_API_AUTO,
        "benchmark": CODEX_WIRE_API_AUTO,
        "local": CODEX_WIRE_API_PROFILE,
        "use_profile": CODEX_WIRE_API_PROFILE,
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
        return settings, False

    if permissions.get("defaultMode") != "bypassPermissions":
        return settings, False

    permissions = dict(permissions)
    permissions["defaultMode"] = "default"
    settings["permissions"] = permissions
    settings["skipDangerousModePermissionPrompt"] = False
    return settings, True


def _make_vscode_settings_root_safe(settings: dict, ssh_profile, client=None) -> tuple[dict, bool]:
    if not _is_root_ssh_user(ssh_profile, client):
        return settings, False

    settings = dict(settings)
    changed = False
    if settings.get("claudeCode.initialPermissionMode") != "default":
        settings["claudeCode.initialPermissionMode"] = "default"
        changed = True
    if settings.get("claudeCode.allowDangerouslySkipPermissions") is not False:
        settings["claudeCode.allowDangerouslySkipPermissions"] = False
        changed = True
    return settings, changed


def _sync_remote_vscode_root_safety(client, ssh_profile) -> bool:
    if not _is_root_ssh_user(ssh_profile, client):
        return False

    vscode = remote_config.read_remote_vscode_settings(client) or {}
    vscode, changed = _make_vscode_settings_root_safe(vscode, ssh_profile, client)
    if changed:
        remote_config.write_remote_vscode_settings(client, vscode)
    return changed


def _codex_profile_api_key(profile) -> str:
    return security.get_secret(getattr(profile, "api_key_ref", None)) or ""


def _codex_profile_runtime_env_keys(profile) -> list[str]:
    return ProviderRegistry.get_codex_runtime_env_keys_for_profile(profile)


def _persist_remote_codex_env(client, profile, api_key: str) -> list[str]:
    from core import persistent_env

    env_keys = _codex_profile_runtime_env_keys(profile)
    persistent_env.set_remote_user_env(client, {key: api_key for key in env_keys})
    return env_keys


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
    if wire_api not in {"chat", "responses"}:
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

    payload = {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "timeout": 10,
        "repeat_count": 3,
        "wire_apis": ["chat", "responses"],
    }
    command = (
        'PYTHON_BIN="$(command -v python3 || command -v python || true)"; '
        '[ -n "$PYTHON_BIN" ] || exit 127; '
        f'"$PYTHON_BIN" -c {shlex.quote(_REMOTE_CODEX_WIRE_BENCHMARK_SCRIPT)}'
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


def sync_claude_to_server(ssh_name: str, claude_name: str) -> str:
    """Sync Claude profile to remote server. Returns status message."""
    claude_profile = _find_profile(profile_manager.list_switchable_claude_profiles(), claude_name, "Claude API Profile")
    if not profile_manager.is_third_party_claude_profile(claude_profile):
        raise ValueError("只能同步第三方 Claude API Profile")
    if not (security.get_secret(claude_profile.auth_token_ref) or security.get_secret(getattr(claude_profile, "primary_api_key_ref", None))):
        raise ValueError("Claude API Profile 需要 Auth Token")

    ssh_profile, client = _connect_ssh(ssh_name)

    settings = remote_config.read_remote_claude_settings(client, ssh_profile) or {}
    settings = parser.apply_claude_profile(settings, claude_profile)
    settings, root_adjusted = _make_claude_settings_root_safe(settings, ssh_profile, client)
    vscode_root_adjusted = _sync_remote_vscode_root_safety(client, ssh_profile)
    config = remote_config.read_remote_claude_config(client, ssh_profile) or {}
    config = parser.apply_claude_config(config, claude_profile)

    remote_config.write_remote_claude_settings(client, settings, ssh_profile)
    remote_config.write_remote_claude_config(client, config, ssh_profile)

    logger.info(f"Synced Claude API profile '{claude_name}' to {ssh_profile.host}")
    message = f"已同步 Claude API '{claude_name}' 到 {ssh_profile.host}"
    return f"{message} | {ROOT_BYPASS_ADJUSTED_MESSAGE}" if root_adjusted or vscode_root_adjusted else message


def sync_claude_account_to_server(ssh_name: str, account_name: str) -> str:
    """Sync a saved Claude official account snapshot to the remote server."""
    account = _find_profile(profile_manager.list_claude_account_profiles(), account_name, "Claude 官方账号")
    credentials = profile_manager.load_claude_account_credentials(account)

    ssh_profile, client = _connect_ssh(ssh_name)

    remote_config.write_remote_claude_credentials(client, credentials, ssh_profile)

    settings = remote_config.read_remote_claude_settings(client, ssh_profile) or {}
    settings = parser.clear_claude_api_overrides(settings)
    settings, root_adjusted = _make_claude_settings_root_safe(settings, ssh_profile, client)
    vscode_root_adjusted = _sync_remote_vscode_root_safety(client, ssh_profile)
    remote_config.write_remote_claude_settings(client, settings, ssh_profile)

    config = remote_config.read_remote_claude_config(client, ssh_profile) or {}
    remote_config.write_remote_claude_config(client, parser.clear_claude_config_auth(config), ssh_profile)

    logger.info(f"Synced Claude account '{account_name}' to {ssh_profile.host}")
    message = f"已同步 Claude 账号 '{account_name}' 到 {ssh_profile.host}"
    return f"{message} | {ROOT_BYPASS_ADJUSTED_MESSAGE}" if root_adjusted or vscode_root_adjusted else message


def sync_codex_to_server(ssh_name: str, codex_name: str, wire_api_mode: str | None = CODEX_WIRE_API_AUTO) -> str:
    """Sync Codex profile to remote server. Returns status message."""
    wire_api_mode = normalize_codex_wire_api_mode(wire_api_mode)
    codex_profile = _find_profile(profile_manager.list_switchable_codex_profiles(), codex_name, "Codex API Profile")
    if not profile_manager.is_third_party_codex_profile(codex_profile):
        raise ValueError("只能同步第三方 Codex API Profile")
    api_key = _codex_profile_api_key(codex_profile)
    if not api_key:
        raise ValueError("Codex API Profile 需要 API Key")

    ssh_profile, client = _connect_ssh(ssh_name)

    config = remote_config.read_remote_codex_config(client, ssh_profile) or {}
    config = toml_parser.apply_codex_profile(config, codex_profile)
    if wire_api_mode in CODEX_WIRE_API_VALUES:
        _set_remote_codex_wire_api(config, codex_profile, wire_api_mode)
    remote_config.write_remote_codex_config(client, config, ssh_profile)

    auth = remote_config.read_remote_codex_auth(client, ssh_profile) or {}
    auth = auth_parser.apply_codex_apikey(auth, codex_profile)
    remote_config.write_remote_codex_auth(client, auth, ssh_profile)
    env_keys = _persist_remote_codex_env(client, codex_profile, api_key)
    benchmark = None
    if wire_api_mode == CODEX_WIRE_API_AUTO:
        benchmark = _remote_benchmark_codex_wire_api(client, codex_profile, config, api_key)
        if benchmark.success and benchmark.recommended_wire_api:
            if _set_remote_codex_wire_api(config, codex_profile, benchmark.recommended_wire_api):
                remote_config.write_remote_codex_config(client, config, ssh_profile)

    logger.info(f"Synced Codex API profile '{codex_name}' to {ssh_profile.host}")
    message = f"已同步 Codex API '{codex_name}' 到 {ssh_profile.host} | 已写入远端环境变量 {', '.join(env_keys)}"
    current_wire_api = _remote_codex_current_wire_api(config, codex_profile)
    if benchmark and benchmark.success and benchmark.recommended_wire_api:
        detail = f" | 远端自测已选择 wire_api={benchmark.recommended_wire_api}"
        if benchmark.selected_model:
            detail += f"，模型={benchmark.selected_model}"
        if benchmark.summary:
            detail += f"（{benchmark.summary}）"
        return message + detail
    if benchmark and benchmark.error:
        return message + f" | 远端 wire_api 自测跳过: {benchmark.error}；当前使用 wire_api={current_wire_api}"
    if wire_api_mode in CODEX_WIRE_API_VALUES:
        return message + f" | 已手动选择 wire_api={current_wire_api}"
    return message + f" | 使用本地配置 wire_api={current_wire_api}"


def sync_codex_account_to_server(ssh_name: str, account_name: str) -> str:
    """Sync a saved Codex official account snapshot to the remote server."""
    account = _find_profile(profile_manager.list_codex_account_profiles(), account_name, "Codex 官方账号")
    auth = profile_manager.load_codex_account_auth(account)

    ssh_profile, client = _connect_ssh(ssh_name)

    remote_config.write_remote_codex_auth(client, auth, ssh_profile)

    config = remote_config.read_remote_codex_config(client, ssh_profile) or {}
    remote_config.write_remote_codex_config(client, toml_parser.apply_codex_official_account(config), ssh_profile)

    logger.info(f"Synced Codex account '{account_name}' to {ssh_profile.host}")
    return f"已同步 Codex 账号 '{account_name}' 到 {ssh_profile.host}"


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


def pull_claude_from_server(ssh_name: str) -> str:
    """Pull Claude config from server and save as a profile."""
    ssh_profiles = profile_manager.list_ssh_profiles()
    ssh_profile = next((p for p in ssh_profiles if p.name == ssh_name), None)
    if not ssh_profile:
        raise ValueError(f"SSH server '{ssh_name}' not found")

    client = ssh_manager.connect(ssh_profile)
    settings = remote_config.read_remote_claude_settings(client, ssh_profile)
    config = remote_config.read_remote_claude_config(client, ssh_profile) or {}
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
    primary_key = config.get("primaryApiKey", "")
    provider = profile_manager.detect_claude_provider(settings)
    if provider == "anthropic":
        return "远程 Claude 配置是官方 Anthropic，已跳过；当前只导入第三方 API Profile"

    from models.profile import ClaudeProfile
    token_ref = f"claude:{name}:auth_token"
    primary_ref = f"claude:{name}:primary_api_key"
    if token_value:
        security.set_secret(token_ref, token_value)
    if primary_key:
        security.set_secret(primary_ref, primary_key)

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
        primary_api_key_ref=primary_ref if primary_key else None,
        base_url=env.get("ANTHROPIC_BASE_URL", ""),
        model=settings.get("model", ""),
        effort_level=settings.get("effortLevel", "high"),
        permissions_mode=permissions.get("defaultMode", "default"),
        skip_dangerous_prompt=settings.get("skipDangerousModePermissionPrompt", False),
        permissions_allow=permissions.get("allow", []),
        additional_directories=additional_directories,
        provider=provider,
    )
    profile_manager.save_claude_profile(profile)

    return f"已从 {ssh_profile.host} 拉取 Claude 配置，保存为 '{name}'"


def pull_codex_from_server(ssh_name: str) -> str:
    """Pull Codex config from server and save as a profile."""
    ssh_profiles = profile_manager.list_ssh_profiles()
    ssh_profile = next((p for p in ssh_profiles if p.name == ssh_name), None)
    if not ssh_profile:
        raise ValueError(f"SSH server '{ssh_name}' not found")

    client = ssh_manager.connect(ssh_profile)

    config = remote_config.read_remote_codex_config(client, ssh_profile)
    auth = remote_config.read_remote_codex_auth(client, ssh_profile)

    if not config and not auth:
        return "服务器上未找到 Codex 配置"

    name = f"Remote-{ssh_name}"
    provider_id = config.get("model_provider", "openai") if config else "openai"
    if provider_id == "openai":
        return "远程 Codex 配置是官方 OpenAI，已跳过；当前只导入第三方 API Profile"
    if not auth or auth.get("OPENAI_API_KEY") is None:
        return "远程 Codex 配置没有 API Key，已跳过；当前只导入第三方 API Profile"

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
            profile_kwargs["custom_wire_api"] = custom.get("wire_api")
            profile_kwargs["custom_env_key"] = custom.get("env_key")
            profile_kwargs["custom_requires_openai_auth"] = custom.get("requires_openai_auth", False)

    ref = f"codex:{name}:api_key"
    security.set_secret(ref, auth["OPENAI_API_KEY"])
    profile_kwargs["api_key_ref"] = ref

    profile = CodexProfile(**profile_kwargs)
    profile_manager.save_codex_profile(profile)

    return f"已从 {ssh_profile.host} 拉取 Codex 配置，保存为 '{name}'"
