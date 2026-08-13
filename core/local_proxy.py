from __future__ import annotations

import atexit
import copy
import gzip
import hashlib
import http.client
import ipaddress
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from config.paths import STORAGE_DIR
from core import persistent_env, remote_proxy, vscode_parser
from core.atomic_io import atomic_write_bytes, atomic_write_text
from core.local_proxy_constants import LOCAL_PROXY_BUILTIN_SITE_IDS, LOCAL_PROXY_BUILTIN_SITES


DEFAULT_LOCAL_MIXED_PORT = 17897
LOCAL_PORT_CANDIDATES = tuple(range(DEFAULT_LOCAL_MIXED_PORT, DEFAULT_LOCAL_MIXED_PORT + 50))
LOCAL_PROXY_DIR = STORAGE_DIR / "local_ai_proxy"
LOCAL_PROXY_CONFIG_DIR = LOCAL_PROXY_DIR / "mihomo"
LOCAL_PROXY_BIN_DIR = LOCAL_PROXY_DIR / "bin"
LOCAL_PROXY_STATE_PATH = LOCAL_PROXY_DIR / "state.json"
LOCAL_PROXY_PREFS_PATH = LOCAL_PROXY_DIR / "preferences.json"
LOCAL_PROXY_LOG_PATH = LOCAL_PROXY_DIR / "mihomo.log"
LOCAL_PROXY_PID_PATH = LOCAL_PROXY_DIR / "mihomo.pid"
LOCAL_PROXY_APPLIED_CONFIG_STATE_KEYS = (
    "applied_config_sha256",
    "applied_config_pid",
    "applied_config_mixed_port",
    "applied_config_at",
)
MIHOMO_DOWNLOAD_RETRIES = 3
WINDOWS_SYSTEM_PROXY_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
WINDOWS_SYSTEM_PROXY_KEYS = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL", "AutoDetect")
WINDOWS_SYSTEM_PROXY_OVERRIDE = "<local>;127.0.0.1;localhost;::1"
LOCAL_AI_PROBE_TARGETS = (
    ("OpenAI/ChatGPT", "https://chatgpt.com/cdn-cgi/trace"),
    ("Claude/Anthropic", "https://api.anthropic.com/"),
    ("Gemini/Google AI", "https://generativelanguage.googleapis.com/"),
)
LOCAL_AI_STABILITY_TARGETS = (
    ("OpenAI API", "https://api.openai.com/v1/models"),
    ("ChatGPT 出口", "https://chatgpt.com/cdn-cgi/trace"),
    ("Claude/Anthropic", "https://api.anthropic.com/v1/models"),
    ("Gemini/Google AI", "https://generativelanguage.googleapis.com/v1beta/models"),
)
LOCAL_AI_STABILITY_ROUNDS = 3
LOCAL_AI_STABILITY_MIN_SERVICE_SUCCESSES = 2
LOCAL_AI_STABILITY_MIN_TOTAL_SUCCESSES = 11
LOCAL_CODEX_COMPACT_PROBE_URL = "https://api.openai.com/v1/responses/compact"
LOCAL_CODEX_COMPACT_PROBE_ATTEMPTS = 1
LOCAL_CODEX_COMPACT_PROBE_PAYLOAD_BYTES = 128 * 1024
LOCAL_PROXY_DEEP_PROBE_MAX_CANDIDATES = 5
LOCAL_PROXY_DEEP_PROBE_ROUNDS = 2
LOCAL_PROXY_DEEP_DOWNLOAD_BYTES = 1024 * 1024
LOCAL_PROXY_DEEP_UPLOAD_BYTES = 512 * 1024
LOCAL_PROXY_DEEP_DOWNLOAD_URL = "https://speed.cloudflare.com/__down?bytes=1048576"
LOCAL_PROXY_DEEP_UPLOAD_URL = "https://speed.cloudflare.com/__up"
LOCAL_PROXY_DEEP_OPERATION_DEADLINE_SECONDS = 25.0
LOCAL_PROXY_DEEP_IO_CHUNK_BYTES = 64 * 1024
LOCAL_PROXY_TCP_PREFILTER_TYPES = frozenset(
    {"http", "https", "socks5", "ss", "ssr", "trojan", "vmess", "vless"}
)
LOCAL_PROXY_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9][a-z0-9-]{0,62})+$",
    re.IGNORECASE,
)
_LOCAL_PROXY_PREFS_LOCK = threading.RLock()
_LOCAL_PROXY_PREFS_CACHE: dict | None = None
_LOCAL_PROXY_PREFS_CACHE_SIGNATURE: tuple[str, int | None, int | None] | None = None
_LOCAL_PROXY_STATE_LOCK = threading.RLock()
_LOCAL_PROXY_STATE_CACHE: dict | None = None
_LOCAL_PROXY_STATE_CACHE_SIGNATURE: tuple[str, int | None, int | None, str] | None = None
_ISOLATED_MIHOMO_LOCK = threading.RLock()
_ISOLATED_MIHOMO_PROCESSES: set[object] = set()
_ISOLATED_MIHOMO_DIRECTORIES: set[Path] = set()
_ISOLATED_MIHOMO_SHUTTING_DOWN = threading.Event()


@dataclass(frozen=True)
class LocalAIProxyStatus:
    installed: bool
    running: bool
    config_path: str
    proxy_url: str
    detail: str = ""
    # Defaults preserve compatibility with callers/tests that construct the
    # status positionally.  ``desired`` comes from preferences, while
    # ``active`` is trusted only after validating the running managed config.
    strict_privacy_active: bool = False
    strict_privacy_desired: bool = False

    def summary(self) -> str:
        state = "运行中" if self.running else "未运行"
        installed = "已配置" if self.installed else "未配置"
        detail = f"；{self.detail}" if self.detail else ""
        return f"本机 AI 代理{installed}，{state}: {self.proxy_url}{detail}"


@dataclass(frozen=True)
class LocalAIProxyProbeResult:
    label: str
    ok: bool
    status: int | None = None
    detail: str = ""
    elapsed_ms: int = 0
    exit_country: str = ""

    def summary(self) -> str:
        prefix = "可达" if self.ok else "失败"
        status = f"HTTP {self.status}" if self.status else self.detail
        elapsed = f"{self.elapsed_ms}ms" if self.elapsed_ms else ""
        pieces = [piece for piece in (prefix, status, elapsed) if piece]
        return f"{self.label}: {' / '.join(pieces)}"


@dataclass(frozen=True)
class LocalProxyNodeStabilityResult:
    node_key: str
    stable: bool
    short_stable: bool = False
    service_successes: dict[str, int] = field(default_factory=dict)
    total_successes: int = 0
    total_attempts: int = 0
    openai_successes: int = 0
    exit_country: str = ""
    application_latency_ms: int | None = None
    codex_compact_ok: bool = False
    codex_compact_successes: int = 0
    codex_compact_attempts: int = 0
    codex_compact_median_ms: int | None = None
    codex_compact_jitter_ms: int | None = None
    codex_compact_detail: str = ""
    deep_transport_ok: bool = False
    deep_transport_successes: int = 0
    deep_transport_attempts: int = 0
    deep_download_bytes: int = 0
    deep_upload_bytes: int = 0
    deep_transport_median_ms: int | None = None
    deep_transport_detail: str = ""
    detail: str = ""

    def summary(self) -> str:
        prefix = "稳定验证通过" if self.stable else "稳定验证失败"
        detail = f"；{self.detail}" if self.detail else ""
        return f"{prefix}: {self.total_successes}/{self.total_attempts}{detail}"


@dataclass(frozen=True)
class CodexCompactTransportProbeResult:
    """Unauthenticated large-request transport approximation for Codex compact."""

    ok: bool
    successes: int = 0
    attempts: int = LOCAL_CODEX_COMPACT_PROBE_ATTEMPTS
    median_ms: int | None = None
    jitter_ms: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class LocalProxyTransferRoundResult:
    ok: bool
    downloaded_bytes: int = 0
    uploaded_bytes: int = 0
    elapsed_ms: int = 0
    detail: str = ""


@dataclass(frozen=True)
class LocalProxyDeepTransportProbeResult:
    ok: bool
    transfer_ok: bool = False
    transfer_successes: int = 0
    transfer_attempts: int = LOCAL_PROXY_DEEP_PROBE_ROUNDS
    downloaded_bytes: int = 0
    uploaded_bytes: int = 0
    median_ms: int | None = None
    jitter_ms: int | None = None
    codex_compact_ok: bool = False
    codex_compact_status: int | None = None
    codex_compact_elapsed_ms: int | None = None
    codex_compact_detail: str = ""
    detail: str = ""


@dataclass(frozen=True)
class _IsolatedMihomoSession:
    proxy_url: str
    config_path: Path
    mixed_port: int
    process: object = field(compare=False, repr=False)


def load_local_proxy_preferences() -> dict:
    with _LOCAL_PROXY_PREFS_LOCK:
        signature = _local_proxy_preferences_signature()
        if _LOCAL_PROXY_PREFS_CACHE is not None and _LOCAL_PROXY_PREFS_CACHE_SIGNATURE == signature:
            return copy.deepcopy(_LOCAL_PROXY_PREFS_CACHE)
        try:
            data = json.loads(LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _quarantine_corrupt_local_json_file(LOCAL_PROXY_PREFS_PATH)
            data = {}
            signature = _local_proxy_preferences_signature()
        except Exception:
            data = {}
        preferences = _normalize_local_proxy_preferences(data)
        _cache_local_proxy_preferences(preferences, signature)
        return copy.deepcopy(preferences)


def _load_local_proxy_routing_preferences_strict() -> dict:
    """Read the on-disk routing authority without a compatibility downgrade.

    A missing preference file is the legacy-compatible first-run state.  Once
    the file exists, however, unreadable, corrupt, or non-object content must
    stop configuration deployment.  In particular, this reader deliberately
    bypasses the permissive UI cache and never quarantines the source file:
    silently replacing an unreadable ``strict_privacy=true`` with defaults
    would turn a requested fail-closed route into ``MATCH,DIRECT``.
    """

    with _LOCAL_PROXY_PREFS_LOCK:
        try:
            content = LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            try:
                quarantined = next(
                    LOCAL_PROXY_PREFS_PATH.parent.glob(
                        f"{LOCAL_PROXY_PREFS_PATH.name}.corrupt-*"
                    ),
                    None,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"无法确认本机代理路由偏好 {LOCAL_PROXY_PREFS_PATH} 的完整性，已中止配置变更"
                ) from exc
            if quarantined is not None:
                raise RuntimeError(
                    f"检测到已隔离的损坏本机代理路由偏好 {quarantined}，已中止配置变更"
                )
            return _normalize_local_proxy_preferences({})
        except Exception as exc:
            raise RuntimeError(
                f"无法读取本机代理路由偏好 {LOCAL_PROXY_PREFS_PATH}，已中止配置变更"
            ) from exc
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"本机代理路由偏好 {LOCAL_PROXY_PREFS_PATH} 已损坏，已中止配置变更"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"本机代理路由偏好 {LOCAL_PROXY_PREFS_PATH} 必须是 JSON 对象，已中止配置变更"
            )
        strict_privacy = _strict_privacy_authority_value(data)
        preferences = _normalize_local_proxy_preferences(data)
        preferences["strict_privacy"] = strict_privacy
        return preferences


def _strict_privacy_authority_value(preferences: dict) -> bool:
    """Parse the persisted privacy intent without treating bad types as off."""

    if "strict_privacy" not in preferences:
        return False
    value = preferences["strict_privacy"]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    elif isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    raise RuntimeError(
        f"本机代理路由偏好 {LOCAL_PROXY_PREFS_PATH} 的 strict_privacy 值无效，"
        "已中止配置变更"
    )


def local_proxy_strict_privacy_desired_authoritative() -> bool:
    """Return the persisted strict-privacy intent without downgrading errors.

    Browser launch uses this authority before deciding whether a managed
    Chrome/Edge process may start without explicit proxy flags.  A missing
    preference file remains the legacy-compatible ``False`` default, while a
    present but unreadable/corrupt file raises so callers can fail closed.
    """

    preferences = _load_local_proxy_routing_preferences_strict()
    return _coerce_bool(preferences.get("strict_privacy"))


def save_local_proxy_preferences(**updates) -> dict:
    with _LOCAL_PROXY_PREFS_LOCK:
        preferences = load_local_proxy_preferences()
        preferences.update({key: value for key, value in updates.items() if value is not None})
        preferences = _normalize_local_proxy_preferences(preferences)
        preferences["updated_at"] = remote_proxy._now_iso()
        _ensure_local_dirs()
        atomic_write_text(
            LOCAL_PROXY_PREFS_PATH,
            json.dumps(preferences, ensure_ascii=False, indent=2),
        )
        _cache_local_proxy_preferences(preferences)
        return copy.deepcopy(preferences)


def clear_local_proxy_preferences_cache() -> None:
    global _LOCAL_PROXY_PREFS_CACHE, _LOCAL_PROXY_PREFS_CACHE_SIGNATURE
    with _LOCAL_PROXY_PREFS_LOCK:
        _LOCAL_PROXY_PREFS_CACHE = None
        _LOCAL_PROXY_PREFS_CACHE_SIGNATURE = None


def clear_local_proxy_state_cache() -> None:
    global _LOCAL_PROXY_STATE_CACHE, _LOCAL_PROXY_STATE_CACHE_SIGNATURE
    with _LOCAL_PROXY_STATE_LOCK:
        _LOCAL_PROXY_STATE_CACHE = None
        _LOCAL_PROXY_STATE_CACHE_SIGNATURE = None


def local_proxy_subscription_direct_fallback_allowed() -> bool:
    """Return the persisted subscription fallback policy, failing closed.

    Subscription downloads run on worker threads after the UI preference
    mirror may have changed.  Read the authoritative file under the same lock
    as preference writes; a present but unreadable/corrupt file must never
    silently downgrade strict privacy to a direct request.
    """

    with _LOCAL_PROXY_PREFS_LOCK:
        try:
            if not LOCAL_PROXY_PREFS_PATH.exists():
                try:
                    quarantined = next(
                        LOCAL_PROXY_PREFS_PATH.parent.glob(
                            f"{LOCAL_PROXY_PREFS_PATH.name}.corrupt-*"
                        ),
                        None,
                    )
                except Exception:
                    return False
                if quarantined is not None:
                    return False
                return True
            raw = json.loads(LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict):
            return False
        try:
            strict_privacy = _strict_privacy_authority_value(raw)
        except Exception:
            return False
        return not strict_privacy


def local_proxy_start_on_login_enabled() -> bool:
    return bool(load_local_proxy_preferences().get("start_on_login"))


def set_local_proxy_start_on_login(enabled: bool) -> dict:
    return save_local_proxy_preferences(start_on_login=_coerce_bool(enabled))


def local_proxy_keep_running_on_exit_enabled() -> bool:
    return bool(load_local_proxy_preferences().get("keep_running_on_exit", True))


def set_local_proxy_keep_running_on_exit(enabled: bool) -> dict:
    return save_local_proxy_preferences(keep_running_on_exit=_coerce_bool(enabled))


def set_local_proxy_startup_node(proxy_text: str) -> str:
    proxy_node = remote_proxy.parse_proxy_node(proxy_text)
    _save_last_proxy_node_strict(proxy_node)
    return remote_proxy.describe_proxy_node(proxy_node)


def local_proxy_startup_node_summary() -> str:
    node = _load_last_proxy_node()
    return remote_proxy.describe_proxy_node(node) if node else ""


def set_local_proxy_non_cn_mode(enabled: bool) -> dict:
    return save_local_proxy_preferences(proxy_non_cn=_coerce_bool(enabled))


def set_local_proxy_strict_privacy(enabled: bool) -> dict:
    """Fail closed for public traffic that enters the managed mihomo proxy.

    This preference intentionally does not claim system-wide/TUN coverage.
    Applications that ignore the Windows/application proxy remain outside the
    managed proxy boundary.
    """

    return save_local_proxy_preferences(strict_privacy=_coerce_bool(enabled))


def set_local_proxy_strict_privacy_and_apply(enabled: bool) -> str:
    """Persist strict privacy and transactionally apply it when running.

    ``reload_local_ai_proxy`` already restores the previous config when its
    controller update fails.  This wrapper restores the matching preference as
    well, so a failed hot update cannot leave the UI claiming a mode that is
    not active.  When the managed proxy is stopped, the preference is simply
    kept for the next start.
    """

    desired = _coerce_bool(enabled)
    previous_preferences = _load_local_proxy_routing_preferences_strict()
    previous_desired = _coerce_bool(previous_preferences.get("strict_privacy"))
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    was_running = _managed_local_proxy_is_running(state) and _is_port_listening(mixed_port)
    config_path = Path(state.get("config_path") or (LOCAL_PROXY_CONFIG_DIR / "config.yaml"))
    previous_config = ""
    if was_running:
        try:
            previous_config = config_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            previous_config = ""

    save_local_proxy_preferences(strict_privacy=desired)
    if not was_running:
        mode = "开启" if desired else "关闭"
        return f"严格隐私偏好已{mode}；本机代理未运行，将在下次启动时生效"

    try:
        apply_message = apply_local_proxy_routing_to_running()
        status = inspect_local_ai_proxy(mixed_port)
        if not status.running:
            raise RuntimeError("热更新后受管代理已停止或端口失去监听")
        if status.strict_privacy_active != desired:
            actual = "严格" if status.strict_privacy_active else "兼容分流"
            expected = "严格" if desired else "兼容分流"
            raise RuntimeError(
                f"热更新后配置校验不一致（期望 {expected}，实际 {actual}）"
            )
    except Exception as exc:
        rollback_errors = []
        rollback_notes = []
        try:
            save_local_proxy_preferences(strict_privacy=previous_desired)
        except Exception as rollback_exc:
            rollback_errors.append(f"偏好回滚失败: {rollback_exc}")
        else:
            rollback_notes.append("已恢复原偏好")

        config_changed = False
        if previous_config:
            try:
                current_config = (
                    config_path.read_text(encoding="utf-8", errors="replace")
                    if config_path.exists()
                    else ""
                )
                config_changed = current_config != previous_config
                if config_changed:
                    atomic_write_text(config_path, previous_config)
            except Exception as rollback_exc:
                rollback_errors.append(f"原配置文件回滚失败: {rollback_exc}")

        current_state = _load_state()
        still_running = _managed_local_proxy_is_running(current_state) and _is_port_listening(
            mixed_port
        )
        current_pid = _read_pid()
        if previous_config and still_running:
            try:
                already_confirmed = _applied_local_config_matches(
                    current_state,
                    config_path,
                    mixed_port,
                    pid=current_pid,
                )
                if config_changed or not already_confirmed:
                    _reload_local_mihomo_config(config_path, mixed_port)
                    _stamp_applied_local_config(
                        current_state,
                        config_path,
                        mixed_port,
                        pid=current_pid,
                    )
                    _save_state(current_state)
                rollback_notes.append("已恢复原运行配置")
            except Exception as rollback_exc:
                rollback_errors.append(f"原运行配置重载失败: {rollback_exc}")
                try:
                    _clear_applied_local_config(current_state)
                    _save_state(current_state)
                except Exception as state_exc:
                    rollback_errors.append(f"清除未确认配置指纹失败: {state_exc}")
        elif previous_config:
            rollback_notes.append("已恢复原配置文件，但代理已停止，需重新启动")
            try:
                _clear_applied_local_config(current_state)
                _save_state(current_state)
            except Exception as state_exc:
                rollback_errors.append(f"清除已停止进程的配置指纹失败: {state_exc}")
        else:
            rollback_notes.append("未读取到可回滚的原配置快照")
            try:
                _clear_applied_local_config(current_state)
                _save_state(current_state)
            except Exception as state_exc:
                rollback_errors.append(f"清除未确认配置指纹失败: {state_exc}")

        rollback_detail = "；".join((*rollback_notes, *rollback_errors))
        suffix = f"；{rollback_detail}" if rollback_detail else ""
        raise RuntimeError(f"应用严格隐私失败: {exc}{suffix}") from exc

    mode = "开启" if desired else "关闭"
    return f"已{mode}严格隐私（应用层）；{apply_message}"


def set_builtin_proxy_site_enabled(site_id: str, enabled: bool) -> dict:
    site_key = str(site_id or "").strip()
    if site_key not in LOCAL_PROXY_BUILTIN_SITE_IDS:
        raise ValueError(f"未知内置站点: {site_id}")
    preferences = load_local_proxy_preferences()
    builtin_sites = dict(preferences.get("builtin_sites") or {})
    builtin_sites[site_key] = _coerce_bool(enabled)
    return save_local_proxy_preferences(builtin_sites=builtin_sites)


def add_custom_proxy_target(target: str, enabled: bool = True) -> dict:
    normalized = normalize_proxy_target(target)
    preferences = load_local_proxy_preferences()
    entries = list(preferences.get("custom_targets") or [])
    for entry in entries:
        if (
            str(entry.get("kind") or "") == normalized["kind"]
            and str(entry.get("value") or "").casefold() == normalized["value"].casefold()
        ):
            entry["enabled"] = _coerce_bool(enabled, True)
            entry["target"] = normalized["target"]
            save_local_proxy_preferences(custom_targets=entries)
            return entry
    entry = {
        "id": uuid.uuid4().hex,
        "target": normalized["target"],
        "kind": normalized["kind"],
        "value": normalized["value"],
        "enabled": _coerce_bool(enabled, True),
        "created_at": remote_proxy._now_iso(),
    }
    entries.append(entry)
    save_local_proxy_preferences(custom_targets=entries)
    return entry


def remove_custom_proxy_target(target_id: str) -> bool:
    target_key = str(target_id or "").strip()
    if not target_key:
        return False
    preferences = load_local_proxy_preferences()
    entries = [entry for entry in preferences.get("custom_targets") or [] if str(entry.get("id") or "") != target_key]
    removed = len(entries) != len(preferences.get("custom_targets") or [])
    if removed:
        save_local_proxy_preferences(custom_targets=entries)
    return removed


def set_custom_proxy_target_enabled(target_id: str, enabled: bool) -> dict:
    target_key = str(target_id or "").strip()
    preferences = load_local_proxy_preferences()
    entries = list(preferences.get("custom_targets") or [])
    for entry in entries:
        if str(entry.get("id") or "") == target_key:
            entry["enabled"] = _coerce_bool(enabled, True)
            save_local_proxy_preferences(custom_targets=entries)
            return entry
    raise ValueError("没有找到要修改的自定义代理目标")


def normalize_proxy_target(target: str) -> dict:
    raw = str(target or "").strip().strip("\"'")
    if not raw:
        raise ValueError("请先输入要代理的网址或 IP")
    if any(marker in raw for marker in ("\n", "\r", ",")):
        raise ValueError("每次只能添加一个网址或 IP")

    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        network = None
    if network is not None:
        return {
            "target": str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network),
            "kind": "ip-cidr",
            "value": str(network),
        }

    host = _extract_host_from_target(raw)
    try:
        network = ipaddress.ip_network(host, strict=False)
    except ValueError:
        network = None
    if network is not None:
        return {
            "target": str(network.network_address) if network.prefixlen == network.max_prefixlen else str(network),
            "kind": "ip-cidr",
            "value": str(network),
        }
    domain = _normalize_domain_target(host)
    return {
        "target": domain,
        "kind": "domain",
        "value": domain,
    }


def apply_local_proxy_routing_to_running() -> str:
    # Validate the persisted routing authority before entering the reload path.
    # ``reload_local_ai_proxy`` reads it again immediately before building the
    # config, closing the window where a corrupt file could otherwise downgrade
    # strict privacy through the permissive UI reader.
    _load_local_proxy_routing_preferences_strict()
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    if not _managed_local_proxy_is_running(state) or not _is_port_listening(mixed_port):
        return "代理范围已保存；本机代理未运行，下次启动时生效"
    node = _read_local_managed_proxy_node() or _load_last_proxy_node()
    if not node:
        raise RuntimeError("未读取到当前运行节点，无法热更新代理范围")
    return reload_local_ai_proxy(remote_proxy.format_proxy_node(node))


def auto_start_local_ai_proxy_if_enabled() -> str:
    preferences = _load_local_proxy_routing_preferences_strict()
    if not _coerce_bool(preferences.get("start_on_login")):
        return "Win11 本机代理自启未开启"
    if os.name != "nt":
        return "本机 AI 代理目前只支持 Windows，已跳过自启"
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    if _managed_local_proxy_is_running(state) and _is_port_listening(mixed_port):
        return f"Win11 本机代理已在运行: {_proxy_url(mixed_port)}"
    node = preferences.get("last_node") or _read_local_managed_proxy_node()
    if not node:
        return "Win11 本机代理自启已开启，但还没有保存过可启动节点"
    return install_local_ai_proxy(remote_proxy.format_proxy_node(node))


def install_local_ai_proxy(proxy_text: str, mixed_port: int = DEFAULT_LOCAL_MIXED_PORT) -> str:
    if os.name != "nt":
        raise RuntimeError("本机 AI 代理目前只支持 Windows")
    # This preflight must precede directory/binary/state/config/process/env
    # mutations.  A corrupt strict-privacy authority is a hard stop.
    routing_preferences = _load_local_proxy_routing_preferences_strict()
    mixed_port = _select_local_mixed_port(mixed_port)
    proxy_node = remote_proxy.parse_proxy_node(proxy_text)
    _ensure_local_dirs()
    config_path = LOCAL_PROXY_CONFIG_DIR / "config.yaml"
    binary_path = _ensure_mihomo_binary()
    proxy_url = _proxy_url(mixed_port)

    state = _load_state()
    if not isinstance(state.get("previous_env"), dict):
        state["previous_env"] = _capture_previous_env()
    if not isinstance(state.get("previous_vscode"), dict):
        state["previous_vscode"] = _capture_vscode_proxy_state(vscode_parser.read_vscode_settings())
    if not isinstance(state.get("previous_system_proxy"), dict):
        state["previous_system_proxy"] = _capture_windows_system_proxy_state()
    state.update(
        {
            "mixed_port": mixed_port,
            "proxy_url": proxy_url,
            "config_path": str(config_path),
            "binary_path": str(binary_path),
            "controller_port": remote_proxy.mihomo_controller_port(mixed_port),
            "installing": True,
            "updated_at": remote_proxy._now_iso(),
        }
    )
    _save_state(state)

    try:
        config_path.write_text(
            _build_local_mihomo_config(
                proxy_node,
                mixed_port,
                preferences=routing_preferences,
            ),
            encoding="utf-8",
        )
        _start_local_mihomo(binary_path, mixed_port)
        applied_pid = _read_pid()
        applied_config_sha256 = _local_config_sha256(config_path)
        if not applied_pid or not applied_config_sha256:
            raise RuntimeError("本机代理已启动，但无法记录已应用配置指纹")
        _apply_local_env(mixed_port)
        _apply_local_vscode_proxy(mixed_port)
        _apply_windows_system_proxy(mixed_port)
    except Exception as exc:
        restore_errors = _restore_managed_settings(state, mixed_port)
        _cleanup_managed_process(binary_path, state)
        _save_restore_retry_state(state, mixed_port, restore_errors)
        message = str(exc)
        if restore_errors:
            message = f"{message}；恢复启动前设置时也遇到问题: {'; '.join(restore_errors)}"
        raise RuntimeError(message) from exc

    state.update(
        {
            "mixed_port": mixed_port,
            "proxy_url": proxy_url,
            "config_path": str(config_path),
            "binary_path": str(binary_path),
            "pid": applied_pid,
            "node_display": remote_proxy.describe_proxy_node(proxy_node),
            "node_key": remote_proxy.proxy_node_key(proxy_node),
            "node_name": str(proxy_node.get("name") or ""),
            "controller_port": remote_proxy.mihomo_controller_port(mixed_port),
            "installing": False,
            "updated_at": remote_proxy._now_iso(),
        }
    )
    _stamp_applied_local_config(
        state,
        config_path,
        mixed_port,
        pid=applied_pid,
        config_sha256=applied_config_sha256,
    )
    _save_state(state)
    _save_last_proxy_node(proxy_node)
    return (
        f"本机 AI 代理已启动: {proxy_url}；"
        "已写入 Windows 用户环境变量、VS Code 本机设置和当前用户系统代理，"
        "并临时关闭系统 PAC/自动检测代理；新终端或重开的 VS Code 窗口生效"
    )


def _select_stable_automatic_local_candidate(
    nodes,
    quality_results: dict[str, remote_proxy.ProxyNodeQualityResult | dict] | None = None,
    *,
    max_candidates: int = 10,
    exclude_keys=(),
) -> tuple[
    remote_proxy.ProxySubscriptionNode | None,
    LocalProxyNodeStabilityResult | None,
    dict[str, LocalProxyNodeStabilityResult],
    dict[str, remote_proxy.ProxyNodeLatencyResult | dict],
]:
    """Deep-validate an automatic candidate without changing the managed proxy."""

    excluded = {str(key or "") for key in (exclude_keys or ()) if str(key or "")}
    candidates = tuple(
        item
        for item in remote_proxy.automatic_proxy_subscription_nodes(nodes, quality_results)
        if remote_proxy.proxy_subscription_node_key(item) not in excluded
    )
    if not candidates:
        return None, None, {}, {}

    tcp_candidates = tuple(
        item for item in candidates if _proxy_node_uses_known_tcp_transport(item.node)
    )
    latencies: dict[str, remote_proxy.ProxyNodeLatencyResult | dict] = {}
    if tcp_candidates:
        latencies = remote_proxy.measure_proxy_node_latencies(
            tcp_candidates,
            timeout=3.0,
            attempts=3,
            max_workers=20,
            require_all=True,
        )
    selected, results = select_stable_local_proxy_node(
        candidates,
        latencies,
        quality_results,
        rounds=LOCAL_AI_STABILITY_ROUNDS,
        max_candidates=max_candidates,
    )
    selected_result = (
        results.get(remote_proxy.proxy_subscription_node_key(selected))
        if selected is not None
        else None
    )
    return selected, selected_result, results, latencies


def _prevalidated_local_candidate_matches(
    proxy_node: dict,
    result: LocalProxyNodeStabilityResult | None,
) -> bool:
    if not isinstance(result, LocalProxyNodeStabilityResult):
        return False
    attempts = int(result.deep_transport_attempts or 0)
    return bool(
        result.node_key == remote_proxy.proxy_node_key(proxy_node)
        and result.stable
        and result.short_stable
        and result.deep_transport_ok
        and attempts >= LOCAL_PROXY_DEEP_PROBE_ROUNDS
        and int(result.deep_transport_successes or 0) == attempts
        and result.codex_compact_ok
    )


def install_local_ai_proxy_verified(
    proxy_text: str,
    candidate_nodes=None,
    max_candidates: int = 10,
    quality_results: dict[str, remote_proxy.ProxyNodeQualityResult | dict] | None = None,
) -> str:
    state = _load_state()
    running_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    if _managed_local_proxy_is_running(state) and _is_port_listening(running_port):
        # The Start action is also safe when a managed proxy already exists:
        # keep the user's manual-node semantics, but use the transactional hot
        # update path instead of terminating and redeploying the working proxy.
        return reload_local_ai_proxy_verified(
            proxy_text,
            candidate_nodes,
            max_candidates=max_candidates,
            quality_results=quality_results,
            automatic_update=False,
        )
    requested_node = remote_proxy.parse_proxy_node(proxy_text)
    requested_key = remote_proxy.proxy_node_key(requested_node)
    install_message = install_local_ai_proxy(proxy_text)
    probe_message = probe_local_ai_proxy()
    if remote_proxy._probe_summary_all_ok(probe_message):
        return f"{install_message}；验证通过: {remote_proxy._compact_probe_summary(probe_message)}"

    candidates = tuple(
        item
        for item in remote_proxy.automatic_proxy_subscription_nodes(
            candidate_nodes,
            quality_results,
        )
        if remote_proxy.proxy_subscription_node_key(item) != requested_key
    )
    if not candidates:
        return f"{install_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}"

    try:
        selected, selected_result, _results, latencies = _select_stable_automatic_local_candidate(
            candidates,
            quality_results,
            max_candidates=max_candidates,
        )
    except Exception as exc:
        return (
            f"{install_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}；"
            f"自动备用节点隔离深测失败，已保留当前启动节点: {exc}"
        )
    if selected is None or not _prevalidated_local_candidate_matches(
        selected.node,
        selected_result,
    ):
        return (
            f"{install_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}；"
            "自动备用节点均未通过 Codex 长会话网络深测，已保留当前启动节点"
        )

    node_summary = remote_proxy.describe_proxy_node(selected.node)
    latency_label = remote_proxy.proxy_node_latency_label(
        latencies.get(remote_proxy.proxy_subscription_node_key(selected))
    )
    try:
        install_local_ai_proxy(remote_proxy.format_proxy_node(selected.node))
        candidate_probe = probe_local_ai_proxy()
    except Exception as exc:
        candidate_probe = f"候选应用或验证执行失败: {exc}"
    if remote_proxy._probe_summary_all_ok(candidate_probe):
        _remember_selected_subscription_node(selected.node)
        return (
            f"本机 AI 代理原启动节点验证失败，已自动切换到 {node_summary}"
            f"（本机 TCP {latency_label}）；隔离环境 Codex 长会话网络深测通过；"
            f"应用后验证通过: {remote_proxy._compact_probe_summary(candidate_probe)}"
        )

    restore_error = ""
    try:
        install_local_ai_proxy(remote_proxy.format_proxy_node(requested_node))
    except Exception as exc:
        restore_error = str(exc).splitlines()[0][:240] or type(exc).__name__
    restore_status = (
        f"，但恢复原节点失败: {restore_error}；请重新检查代理状态"
        if restore_error
        else "，已恢复原节点"
    )
    return (
        f"{install_message}；自动备用节点虽通过隔离深测，但应用后验证未完全通过: "
        f"{remote_proxy._compact_probe_summary(candidate_probe)}{restore_status}"
    )


def reload_local_ai_proxy(
    proxy_text: str,
    mixed_port: int = DEFAULT_LOCAL_MIXED_PORT,
    *,
    profile_id: str = "",
) -> str:
    if os.name != "nt":
        raise RuntimeError("本机 AI 代理目前只支持 Windows")
    # Fail before touching the active YAML/controller/state when the persisted
    # routing authority cannot be trusted.
    routing_preferences = _load_local_proxy_routing_preferences_strict()
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or mixed_port,
        "本机代理端口",
    )
    if not _managed_local_proxy_is_running(state) or not _is_port_listening(mixed_port):
        return "本机 AI 代理未运行或不是本工具受管进程，已跳过热更新"
    status = inspect_local_ai_proxy(mixed_port)
    if not status.running:
        return "本机 AI 代理未运行，已跳过热更新"
    proxy_node = remote_proxy.parse_proxy_node(proxy_text)
    config_path = Path(status.config_path)
    old_config = config_path.read_text(encoding="utf-8", errors="replace") if config_path.exists() else ""
    if not old_config.strip():
        raise RuntimeError("未读取到当前受管代理配置，无法保证失败回滚，已跳过热更新")
    new_config = _build_local_mihomo_config(
        proxy_node,
        mixed_port,
        preferences=routing_preferences,
    )
    current_pid = _read_pid()
    same_config = old_config.strip() == new_config.strip()
    if same_config and _applied_local_config_matches(
        state,
        config_path,
        mixed_port,
        pid=current_pid,
    ):
        _remember_selected_subscription_node(proxy_node, profile_id=profile_id)
        return "本机 AI 代理运行节点已是最新配置，无需热更新"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not same_config:
        atomic_write_text(config_path, new_config)
    try:
        _reload_local_mihomo_config(config_path, mixed_port)
    except Exception as exc:
        restore_error = None
        state_error = None
        restored = False
        if old_config:
            try:
                atomic_write_text(config_path, old_config)
                _reload_local_mihomo_config(config_path, mixed_port)
                restored = True
            except Exception as restore_exc:
                restore_error = restore_exc
        try:
            if restored:
                _stamp_applied_local_config(
                    state,
                    config_path,
                    mixed_port,
                    pid=_read_pid(),
                )
            else:
                _clear_applied_local_config(state)
            _save_state(state)
        except Exception as state_exc:
            state_error = state_exc
        suffix = f"；旧配置强制恢复也失败: {restore_error}" if restore_error else "；已强制恢复旧配置"
        if state_error:
            suffix += f"；已应用配置指纹状态更新失败: {state_error}"
        raise RuntimeError(f"当前本机代理不支持无感热更新或控制口不可用: {exc}{suffix}") from exc

    state.update(
        {
            "mixed_port": mixed_port,
            "proxy_url": _proxy_url(mixed_port),
            "config_path": str(config_path),
            "node_display": remote_proxy.describe_proxy_node(proxy_node),
            "node_key": remote_proxy.proxy_node_key(proxy_node),
            "node_name": str(proxy_node.get("name") or ""),
            "controller_port": remote_proxy.mihomo_controller_port(mixed_port),
            "updated_at": remote_proxy._now_iso(),
        }
    )
    _stamp_applied_local_config(
        state,
        config_path,
        mixed_port,
        pid=_read_pid(),
    )
    _save_state(state)
    _save_last_proxy_node(proxy_node)
    _remember_selected_subscription_node(proxy_node, profile_id=profile_id)
    return f"本机 AI 代理已热更新节点为 {remote_proxy.describe_proxy_node(proxy_node)}"


def reload_local_ai_proxy_verified(
    proxy_text: str,
    candidate_nodes=None,
    max_candidates: int = 10,
    quality_results: dict[str, remote_proxy.ProxyNodeQualityResult | dict] | None = None,
    profile_id: str = "",
    *,
    automatic_update: bool = False,
    _prevalidated_result: LocalProxyNodeStabilityResult | None = None,
    _expected_original_node: dict | None = None,
) -> str:
    requested_node = remote_proxy.parse_proxy_node(proxy_text)
    requested_key = remote_proxy.proxy_node_key(requested_node)
    original_node = _read_local_managed_proxy_node()
    if automatic_update and not _prevalidated_local_candidate_matches(
        requested_node,
        _prevalidated_result,
    ):
        return "本机 AI 代理自动更新候选未通过精确匹配的 Codex 长会话网络深测，已保留当前运行节点"
    if automatic_update:
        try:
            expected_key = remote_proxy.proxy_node_key(_expected_original_node or {})
            original_key = remote_proxy.proxy_node_key(original_node or {})
        except Exception:
            expected_key = original_key = ""
        if not expected_key or original_key != expected_key:
            return "本机 AI 代理当前运行节点在深测期间已变化，已放弃自动更新且未切换节点"
    try:
        if profile_id:
            reload_message = reload_local_ai_proxy(proxy_text, profile_id=profile_id)
        else:
            reload_message = reload_local_ai_proxy(proxy_text)
    except Exception as exc:
        return f"本机 AI 代理自动更新跳过，{exc}"
    if "跳过" in reload_message:
        return reload_message

    try:
        probe_message = probe_local_ai_proxy()
    except Exception as exc:
        restore_suffix = _restore_local_proxy_node_after_failed_update(
            original_node,
            requested_node,
            profile_id=profile_id,
        )
        return f"{reload_message}；热更新后验证执行失败: {exc}{restore_suffix}"
    if remote_proxy._probe_summary_all_ok(probe_message):
        prevalidated = "；隔离环境 Codex 长会话网络深测通过" if automatic_update else ""
        return (
            f"{reload_message}{prevalidated}；"
            f"验证通过: {remote_proxy._compact_probe_summary(probe_message)}"
        )

    if automatic_update:
        restore_suffix = _restore_local_proxy_node_after_failed_update(
            original_node,
            requested_node,
            profile_id=profile_id,
        )
        return (
            f"{reload_message}；隔离深测候选应用后验证未完全通过: "
            f"{remote_proxy._compact_probe_summary(probe_message)}{restore_suffix}"
        )

    candidates = tuple(
        item
        for item in remote_proxy.automatic_proxy_subscription_nodes(
            candidate_nodes,
            quality_results,
        )
        if remote_proxy.proxy_subscription_node_key(item) != requested_key
    )
    if not candidates:
        restore_suffix = _restore_local_proxy_node_after_failed_update(
            original_node,
            requested_node,
            profile_id=profile_id,
        )
        return f"{reload_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}{restore_suffix}"

    try:
        selected, selected_result, _results, latencies = _select_stable_automatic_local_candidate(
            candidates,
            quality_results,
            max_candidates=max_candidates,
        )
    except Exception as exc:
        restore_suffix = _restore_local_proxy_node_after_failed_update(
            original_node,
            requested_node,
            profile_id=profile_id,
        )
        return (
            f"{reload_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}；"
            f"自动换节点测速失败: {exc}{restore_suffix}"
        )

    if selected is not None and _prevalidated_local_candidate_matches(
        selected.node,
        selected_result,
    ):
        try:
            if profile_id:
                reload_local_ai_proxy(
                    remote_proxy.format_proxy_node(selected.node),
                    profile_id=profile_id,
                )
            else:
                reload_local_ai_proxy(remote_proxy.format_proxy_node(selected.node))
            candidate_probe = probe_local_ai_proxy()
        except Exception as exc:
            candidate_probe = f"候选应用或验证执行失败: {exc}"
        if remote_proxy._probe_summary_all_ok(candidate_probe):
            _remember_selected_subscription_node(selected.node, profile_id=profile_id)
            result = latencies.get(remote_proxy.proxy_subscription_node_key(selected))
            return (
                f"本机 AI 代理原热更新节点验证失败，已无重启切换到 "
                f"{remote_proxy.describe_proxy_node(selected.node)}"
                f"（本机 TCP {remote_proxy.proxy_node_latency_label(result)}）；"
                "隔离环境 Codex 长会话网络深测通过；"
                f"验证通过: {remote_proxy._compact_probe_summary(candidate_probe)}"
            )

    restore_suffix = _restore_local_proxy_node_after_failed_update(
        original_node,
        requested_node,
        profile_id=profile_id,
    )
    return (
        f"{reload_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}；"
        f"自动备用节点均未通过 Codex 长会话网络深测或应用后验证{restore_suffix}"
    )


def refresh_running_local_ai_proxy_from_subscription(
    nodes,
    quality_results: dict[str, remote_proxy.ProxyNodeQualityResult | dict] | None = None,
    profile_id: str = "",
) -> str:
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    if not _managed_local_proxy_is_running(state) or not _is_port_listening(mixed_port):
        return "本机 AI 代理未运行，已跳过订阅热更新"
    candidates = tuple(item for item in (nodes or []) if isinstance(item, remote_proxy.ProxySubscriptionNode))
    if not candidates:
        return "订阅里没有可用节点，已跳过本机热更新"
    current_node = _read_local_managed_proxy_node()
    if current_node is None:
        return "订阅已刷新，但无法读取当前运行节点，无法保证失败回滚，已保留当前运行节点"
    current_key = ""
    if current_node:
        try:
            current_key = remote_proxy.proxy_node_key(current_node)
        except Exception:
            current_key = ""
    exact_current = next(
        (
            item
            for item in candidates
            if current_key and remote_proxy.proxy_subscription_node_key(item) == current_key
        ),
        None,
    )
    if exact_current is not None:
        return reload_local_ai_proxy_verified(
            remote_proxy.format_proxy_node(exact_current.node),
            candidates,
            quality_results=quality_results,
            profile_id=profile_id,
        )

    automatic_candidates = remote_proxy.automatic_proxy_subscription_nodes(
        candidates,
        quality_results,
    )
    if not automatic_candidates:
        return "订阅已刷新，但仅有香港节点可候选；香港仅允许手动选择，已保留当前运行节点"
    try:
        chosen, selected_result, _results, _latencies = _select_stable_automatic_local_candidate(
            automatic_candidates,
            quality_results,
            max_candidates=10,
            exclude_keys=(current_key,),
        )
    except Exception as exc:
        return f"订阅已刷新，但候选节点隔离深测失败，已保留当前运行节点: {exc}"
    if chosen is None or not _prevalidated_local_candidate_matches(
        chosen.node,
        selected_result,
    ):
        return "订阅已刷新，但没有候选通过 Codex 长会话网络深测，已保留当前运行节点"
    latest_node = _read_local_managed_proxy_node()
    try:
        latest_key = remote_proxy.proxy_node_key(latest_node) if latest_node else ""
    except Exception:
        latest_key = ""
    if not latest_key or latest_key != current_key:
        return "订阅已刷新，但深测期间当前运行节点已变化，已放弃后台结果且未切换节点"
    return reload_local_ai_proxy_verified(
        remote_proxy.format_proxy_node(chosen.node),
        candidates,
        quality_results=quality_results,
        profile_id=profile_id,
        automatic_update=True,
        _prevalidated_result=selected_result,
        _expected_original_node=current_node,
    )


def current_local_ai_proxy_node_key() -> str:
    state = _load_state()
    key = str(state.get("node_key") or "")
    if key:
        return key
    node = _read_local_managed_proxy_node()
    if not node:
        return ""
    try:
        return remote_proxy.proxy_node_key(node)
    except Exception:
        return ""


def inspect_local_ai_proxy(mixed_port: int = DEFAULT_LOCAL_MIXED_PORT) -> LocalAIProxyStatus:
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or mixed_port,
        "本机代理端口",
    )
    config_path = Path(state.get("config_path") or (LOCAL_PROXY_CONFIG_DIR / "config.yaml"))
    pid = _read_pid()
    pid_running = _is_pid_running(pid) if pid else False
    managed_pid_running = bool(pid and pid_running and _is_managed_mihomo_pid(pid, state=state))
    port_listening = _is_port_listening(mixed_port)
    installed = config_path.exists()
    running = managed_pid_running and port_listening
    preferences = load_local_proxy_preferences()
    strict_privacy_desired = _coerce_bool(preferences.get("strict_privacy"))
    strict_config_contract = running and _managed_local_config_has_strict_privacy(config_path)
    applied_config_matches = running and _applied_local_config_matches(
        state,
        config_path,
        mixed_port,
        pid=pid,
    )
    strict_privacy_active = strict_config_contract and applied_config_matches
    details = [
        _local_proxy_privacy_posture_detail(
            desired=strict_privacy_desired,
            active=strict_privacy_active,
            running=running,
            strict_config_unverified=bool(strict_config_contract and not applied_config_matches),
        )
    ]
    if pid_running and not managed_pid_running:
        details.append("pid 文件指向非本工具代理进程")
    if managed_pid_running and not port_listening:
        details.append("受管进程存在，但端口未监听")
    elif installed and port_listening and not managed_pid_running:
        details.append("端口已监听，但 pid 文件未更新或不是本工具进程")
    elif not installed and port_listening:
        details.append("默认端口被其他程序占用，本工具启动时会自动选择空闲端口")
    if managed_pid_running or port_listening:
        details.append("Windows 环境变量已指向本机代理" if _local_env_matches(mixed_port) else "Windows 环境变量未完全指向本机代理")
        details.append("Windows 系统代理已指向本机代理" if _windows_system_proxy_matches(mixed_port) else "Windows 系统代理未指向本机代理")
        vscode_status = _local_vscode_proxy_match_detail(mixed_port)
        if vscode_status:
            details.append(vscode_status)
    elif installed:
        if _local_env_matches(mixed_port):
            details.append("代理未运行，但 Windows 环境变量仍指向本机代理")
        if _windows_system_proxy_matches(mixed_port):
            details.append("代理未运行，但 Windows 系统代理仍指向本机代理")
        vscode_status = _local_vscode_proxy_match_detail(mixed_port)
        if vscode_status.startswith("VS Code 本机设置已"):
            details.append("代理未运行，但 VS Code 本机设置仍指向本机代理")
    return LocalAIProxyStatus(
        installed=installed,
        running=running,
        config_path=str(config_path),
        proxy_url=_proxy_url(mixed_port),
        detail="；".join(details),
        strict_privacy_active=strict_privacy_active,
        strict_privacy_desired=strict_privacy_desired,
    )


def _managed_local_config_has_strict_privacy(config_path: Path) -> bool:
    """Validate local YAML with the shared managed strict-privacy contract."""

    try:
        content = config_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return remote_proxy._managed_config_strict_privacy_enabled(content)


def _local_proxy_privacy_posture_detail(
    *,
    desired: bool,
    active: bool,
    running: bool,
    strict_config_unverified: bool = False,
) -> str:
    if not running:
        if desired:
            return "隐私边界: 严格隐私偏好已保存，代理未运行，待下次启动生效（非 VPN/TUN）"
        return "隐私边界: 代理未运行；兼容分流偏好已保存（非 VPN/TUN）"
    if strict_config_unverified:
        return (
            "隐私边界未确认: 磁盘配置符合严格契约，但当前受管进程缺少匹配的"
            "已应用 SHA-256/PID/端口指纹；不能确认该进程已加载此配置，请重新应用规则或重启本机代理"
        )
    if desired != active:
        if desired:
            return (
                "隐私边界漂移: 偏好要求严格隐私，但当前运行配置未通过 "
                "fail-closed/DNS/IPv6 契约校验，可能仍有 DIRECT；请重新应用规则"
            )
        return (
            "隐私边界漂移: 当前运行配置实际为严格隐私，但偏好要求兼容分流；"
            "请重新应用规则"
        )
    if active:
        return "隐私边界: 当前运行配置已验证应用层严格模式（公网 fail-closed、代理内 DoH、IPv6 已关闭；非 VPN/TUN）"
    return "隐私边界: 当前运行配置允许兼容分流/DIRECT；无法防止系统 DNS、WebRTC/UDP 或忽略代理的程序泄露"


def stop_local_ai_proxy(restore_settings: bool = True) -> str:
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    pid = _read_pid()
    stopped = False
    skipped_unmanaged = False
    if pid and _is_pid_running(pid):
        if _is_managed_mihomo_pid(pid, state=state):
            stopped = _terminate_pid(pid)
        else:
            skipped_unmanaged = True
    LOCAL_PROXY_PID_PATH.unlink(missing_ok=True)
    restore_errors = []
    if restore_settings:
        restore_errors = _restore_managed_settings(state, mixed_port)
    if restore_settings:
        _save_restore_retry_state(state, mixed_port, restore_errors)
    else:
        _save_state({})
    restore_suffix = f"；但恢复设置失败: {'; '.join(restore_errors)}" if restore_errors else ""
    if stopped:
        return f"本机 AI 代理已停止{restore_suffix}"
    if skipped_unmanaged:
        return f"本机 AI 代理未停止：pid 文件指向的进程不是本工具启动的代理，已跳过{restore_suffix}"
    return f"本机 AI 代理未发现运行中的受管进程{restore_suffix}"


def probe_local_ai_proxy(timeout: int = 8) -> str:
    status = inspect_local_ai_proxy()
    if not status.running:
        return f"{status.summary()}；代理未运行，跳过 AI 连通性探测"

    results = [
        _probe_url_through_proxy(status.proxy_url, label, url, timeout=timeout)
        for label, url in LOCAL_AI_PROBE_TARGETS
    ]
    ok_count = sum(1 for item in results if item.ok)
    details = "；".join(item.summary() for item in results)
    return f"{status.summary()}；AI 连通性 {ok_count}/{len(results)} 可达；{details}"


def select_stable_local_proxy_node(
    nodes,
    latency_results: dict[str, remote_proxy.ProxyNodeLatencyResult | dict] | None,
    quality_results: dict[str, remote_proxy.ProxyNodeQualityResult | dict] | None = None,
    *,
    rounds: int = LOCAL_AI_STABILITY_ROUNDS,
    timeout: int = 8,
    max_candidates: int = 10,
) -> tuple[remote_proxy.ProxySubscriptionNode | None, dict[str, LocalProxyNodeStabilityResult]]:
    """Return the first TCP-reachable node that passes isolated AI stability checks.

    The probe process owns a temporary mihomo directory and loopback ports. It
    never calls the managed proxy install/reload/start paths and therefore does
    not change the user's proxy, environment, VS Code settings, state, or PID.
    """

    rounds = max(3, int(rounds or LOCAL_AI_STABILITY_ROUNDS))
    timeout = max(1, min(60, int(timeout or 8)))
    attempts = max(1, min(50, int(max_candidates or 10)))
    tcp_candidates = []
    data_plane_candidates = []
    for source_index, item in enumerate(
        remote_proxy.automatic_proxy_subscription_nodes(nodes, quality_results)
    ):
        key = remote_proxy.proxy_subscription_node_key(item)
        latency = (latency_results or {}).get(key)
        tcp_prefilter_applies = _proxy_node_uses_known_tcp_transport(item.node)
        if tcp_prefilter_applies and not remote_proxy.proxy_node_latency_ok(latency):
            continue
        quality = (quality_results or {}).get(key)
        if (
            remote_proxy.proxy_node_quality_decisive_for_ai_proxy(quality)
            and not remote_proxy.proxy_node_quality_for_ai_proxy_ok(quality)
        ):
            continue
        latency_ms = remote_proxy.proxy_node_latency_ms(latency)
        if latency_ms is not None:
            tcp_candidates.append((latency_ms, key, item))
        elif not tcp_prefilter_applies:
            data_plane_candidates.append((source_index, key, item))

    tcp_candidates.sort(key=lambda value: (value[0], value[1]))
    data_plane_candidates.sort(key=lambda value: (value[0], value[1]))
    tcp_quota = attempts if not data_plane_candidates else max(1, attempts // 2)
    data_plane_quota = attempts if not tcp_candidates else max(1, attempts - tcp_quota)
    selected_tcp = tcp_candidates[:tcp_quota]
    selected_data_plane = data_plane_candidates[:data_plane_quota]
    remaining_slots = attempts - len(selected_tcp) - len(selected_data_plane)
    if remaining_slots > 0:
        selected_tcp.extend(tcp_candidates[len(selected_tcp):][:remaining_slots])
        remaining_slots = attempts - len(selected_tcp) - len(selected_data_plane)
    if remaining_slots > 0:
        selected_data_plane.extend(
            data_plane_candidates[len(selected_data_plane):][:remaining_slots]
        )
    candidates = [item for _latency, _key, item in selected_tcp]
    candidates.extend(item for _index, _key, item in selected_data_plane)

    results: dict[str, LocalProxyNodeStabilityResult] = {}
    if not candidates:
        return None, results

    _ensure_local_dirs()
    binary_path = _ensure_mihomo_binary()
    for item in candidates:
        if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
            break
        key = remote_proxy.proxy_subscription_node_key(item)
        try:
            with _isolated_mihomo_session(binary_path, item.node) as session:
                result = _probe_local_proxy_node_stability(
                    session.proxy_url,
                    key,
                    rounds=rounds,
                    timeout=timeout,
                )
        except Exception as exc:
            result = LocalProxyNodeStabilityResult(
                node_key=key,
                stable=False,
                total_attempts=rounds * len(LOCAL_AI_STABILITY_TARGETS),
                detail=str(exc).splitlines()[0][:240] or type(exc).__name__,
            )
        results[key] = result
    def passed_short_probe(result: LocalProxyNodeStabilityResult | None) -> bool:
        # ``stable`` was the short-probe flag before the deep gate existed.
        # Accept that shape as input for compatibility, but always replace it
        # with the final short + deep verdict below before returning a node.
        return bool(result and (result.short_stable or result.stable))

    short_stable_candidates = [
        item
        for item in candidates
        if passed_short_probe(results.get(remote_proxy.proxy_subscription_node_key(item)))
    ]
    if not short_stable_candidates:
        return None, results

    def short_stable_sort_key(item):
        key = remote_proxy.proxy_subscription_node_key(item)
        result = results[key]
        tcp_latency = remote_proxy.proxy_node_latency_ms((latency_results or {}).get(key))
        application_latency = result.application_latency_ms
        return (
            application_latency if application_latency is not None else 10**9,
            tcp_latency if tcp_latency is not None else 10**9,
            key,
        )

    ranked_short_stable = sorted(short_stable_candidates, key=short_stable_sort_key)
    for item in ranked_short_stable[:LOCAL_PROXY_DEEP_PROBE_MAX_CANDIDATES]:
        if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
            break
        key = remote_proxy.proxy_subscription_node_key(item)
        try:
            with _isolated_mihomo_session(binary_path, item.node) as session:
                deep = _probe_local_proxy_node_deep_transport(
                    session.proxy_url,
                    timeout=timeout,
                )
        except Exception as exc:
            deep = LocalProxyDeepTransportProbeResult(
                ok=False,
                transfer_attempts=LOCAL_PROXY_DEEP_PROBE_ROUNDS,
                detail=str(exc).splitlines()[0][:240] or type(exc).__name__,
            )
        previous = results[key]
        deep_detail = deep.detail or "Codex 长会话网络近似未通过"
        short_probe_ok = passed_short_probe(previous)
        results[key] = replace(
            previous,
            stable=bool(short_probe_ok and deep.ok),
            short_stable=short_probe_ok,
            codex_compact_ok=deep.codex_compact_ok,
            codex_compact_successes=1 if deep.codex_compact_ok else 0,
            codex_compact_attempts=1,
            codex_compact_median_ms=deep.codex_compact_elapsed_ms,
            codex_compact_jitter_ms=None,
            codex_compact_detail=deep.codex_compact_detail,
            deep_transport_ok=deep.transfer_ok,
            deep_transport_successes=deep.transfer_successes,
            deep_transport_attempts=deep.transfer_attempts,
            deep_download_bytes=deep.downloaded_bytes,
            deep_upload_bytes=deep.uploaded_bytes,
            deep_transport_median_ms=deep.median_ms,
            deep_transport_detail=deep.detail,
            detail=(
                f"{previous.detail}；{deep_detail}"
                if previous.detail
                else deep_detail
            )[:900],
        )
        if results[key].stable:
            return item, results
    return None, results


def _proxy_node_uses_known_tcp_transport(node: dict) -> bool:
    try:
        node_type = str(remote_proxy._normalize_proxy_node(node).get("type") or "").casefold()
    except Exception:
        return False
    return node_type in LOCAL_PROXY_TCP_PREFILTER_TYPES


def _probe_local_proxy_node_stability(
    proxy_url: str,
    node_key: str,
    *,
    rounds: int = LOCAL_AI_STABILITY_ROUNDS,
    timeout: int = 8,
) -> LocalProxyNodeStabilityResult:
    rounds = max(3, int(rounds or LOCAL_AI_STABILITY_ROUNDS))
    service_successes = {label: 0 for label, _url in LOCAL_AI_STABILITY_TARGETS}
    exit_countries: list[str] = []
    openai_elapsed_ms: list[int] = []
    failure_details: list[str] = []

    for _round in range(rounds):
        with ThreadPoolExecutor(max_workers=len(LOCAL_AI_STABILITY_TARGETS)) as executor:
            futures = {
                executor.submit(
                    _probe_ai_url_through_explicit_http_proxy,
                    proxy_url,
                    label,
                    url,
                    timeout,
                ): label
                for label, url in LOCAL_AI_STABILITY_TARGETS
            }
            for future in as_completed(futures):
                label = futures[future]
                try:
                    probe = future.result()
                except Exception as exc:
                    probe = LocalAIProxyProbeResult(label, False, detail=str(exc).splitlines()[0][:120])
                if probe.ok:
                    service_successes[label] += 1
                    if label == "OpenAI API":
                        openai_elapsed_ms.append(max(0, int(probe.elapsed_ms or 0)))
                elif len(failure_details) < 4:
                    failure_details.append(probe.summary())
                if probe.exit_country:
                    exit_countries.append(probe.exit_country)

    total_successes = sum(service_successes.values())
    total_attempts = rounds * len(LOCAL_AI_STABILITY_TARGETS)
    openai_successes = service_successes.get("OpenAI API", 0)
    hong_kong_country = next(
        (country for country in exit_countries if remote_proxy.proxy_region_is_hong_kong(country)),
        "",
    )
    exit_country = hong_kong_country or next((country for country in exit_countries if country), "")
    hong_kong_exit = bool(hong_kong_country)
    ordered_openai_elapsed = sorted(openai_elapsed_ms)
    application_latency_ms = (
        ordered_openai_elapsed[len(ordered_openai_elapsed) // 2]
        if ordered_openai_elapsed
        else None
    )
    min_total = max(
        LOCAL_AI_STABILITY_MIN_TOTAL_SUCCESSES,
        total_attempts - 1,
    )
    min_per_service = max(
        LOCAL_AI_STABILITY_MIN_SERVICE_SUCCESSES,
        rounds - 1,
    )
    ai_stable = (
        openai_successes == rounds
        and all(count >= min_per_service for count in service_successes.values())
        and total_successes >= min_total
        and not hong_kong_exit
    )
    stable = False
    if hong_kong_exit:
        detail = f"ChatGPT 实际出口为香港（loc={exit_country}），禁止自动选择"
    elif ai_stable:
        detail = "OpenAI API 全轮通过，各 AI 服务达到短探针稳定阈值"
    else:
        counts = "，".join(f"{label} {count}/{rounds}" for label, count in service_successes.items())
        detail = f"{counts}；总计 {total_successes}/{total_attempts}"
        if failure_details:
            detail += "；" + "；".join(failure_details)
    return LocalProxyNodeStabilityResult(
        node_key=node_key,
        stable=stable,
        short_stable=ai_stable,
        service_successes=service_successes,
        total_successes=total_successes,
        total_attempts=total_attempts,
        openai_successes=openai_successes,
        exit_country=exit_country,
        application_latency_ms=application_latency_ms,
        detail=detail[:900],
    )


def _probe_codex_compact_transport_quality(
    proxy_url: str,
    *,
    timeout: int = 8,
    attempts: int = LOCAL_CODEX_COMPACT_PROBE_ATTEMPTS,
    payload_bytes: int = LOCAL_CODEX_COMPACT_PROBE_PAYLOAD_BYTES,
) -> CodexCompactTransportProbeResult:
    """Preflight the official compact path with fixed unauthenticated JSON.

    The request carries fixed filler only and deliberately omits Authorization,
    so it cannot run a real account compact operation or incur model usage.
    """

    attempts = max(1, min(3, int(attempts or LOCAL_CODEX_COMPACT_PROBE_ATTEMPTS)))
    payload_bytes = max(64 * 1024, min(1024 * 1024, int(payload_bytes or 0)))
    prefix = (
        b'{"model":"api-switcher-network-probe-no-model",'
        b'"input":[{"role":"user","content":"'
    )
    suffix = b'"}]}'
    if payload_bytes <= len(prefix) + len(suffix):
        raise ValueError("Codex 路径预检请求体过小")
    payload = prefix + (b"x" * (payload_bytes - len(prefix) - len(suffix))) + suffix
    elapsed_values = []
    failure_details = []
    for _attempt in range(attempts):
        if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
            failure_details.append("应用正在退出，已取消 compact 路径预检")
            break
        probe = _post_unauthenticated_json_through_explicit_http_proxy(
            proxy_url,
            LOCAL_CODEX_COMPACT_PROBE_URL,
            payload,
            timeout=timeout,
        )
        if probe.ok:
            elapsed_values.append(max(0, int(probe.elapsed_ms or 0)))
        elif len(failure_details) < 3:
            failure_details.append(probe.detail or f"HTTP {probe.status or '无状态'}")

    ordered = sorted(elapsed_values)
    median_ms = ordered[len(ordered) // 2] if ordered else None
    jitter_ms = ordered[-1] - ordered[0] if len(ordered) > 1 else None
    ok = len(ordered) == attempts
    size_kib = payload_bytes // 1024
    if ok:
        detail = (
            f"官方 compact 路径无认证 {size_kib}KiB 预检 "
            f"{attempts}/{attempts} 收到完整结构化 401，中位 {median_ms}ms；"
            "未使用账号或执行真实 compact"
        )
    else:
        detail = f"官方 compact 路径无认证预检仅 {len(ordered)}/{attempts} 完整返回"
        if failure_details:
            detail += "；" + "；".join(failure_details)
    return CodexCompactTransportProbeResult(
        ok=ok,
        successes=len(ordered),
        attempts=attempts,
        median_ms=median_ms,
        jitter_ms=jitter_ms,
        detail=detail[:700],
    )


def _probe_local_proxy_node_deep_transport(
    proxy_url: str,
    *,
    timeout: int = 8,
    rounds: int = LOCAL_PROXY_DEEP_PROBE_ROUNDS,
    transfer_probe=None,
    compact_probe=None,
) -> LocalProxyDeepTransportProbeResult:
    """Run bounded public transfer checks plus an unauthenticated compact preflight."""

    rounds = max(2, min(3, int(rounds or LOCAL_PROXY_DEEP_PROBE_ROUNDS)))
    transfer_probe = transfer_probe or _probe_cloudflare_transfer_round
    compact_probe = compact_probe or _probe_codex_compact_transport_quality
    transfer_results = []
    for _round in range(rounds):
        if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
            transfer_results.append(
                LocalProxyTransferRoundResult(
                    ok=False,
                    detail="应用正在退出，已取消深度传输验证",
                )
            )
            break
        try:
            result = transfer_probe(proxy_url, timeout=timeout)
        except Exception as exc:
            result = LocalProxyTransferRoundResult(
                ok=False,
                detail=str(exc).splitlines()[0][:200] or type(exc).__name__,
            )
        transfer_results.append(result)

    successes = sum(1 for result in transfer_results if result.ok)
    downloaded_bytes = sum(max(0, int(result.downloaded_bytes or 0)) for result in transfer_results)
    uploaded_bytes = sum(max(0, int(result.uploaded_bytes or 0)) for result in transfer_results)
    elapsed_values = sorted(
        max(0, int(result.elapsed_ms or 0))
        for result in transfer_results
        if result.ok
    )
    median_ms = elapsed_values[len(elapsed_values) // 2] if elapsed_values else None
    jitter_ms = elapsed_values[-1] - elapsed_values[0] if len(elapsed_values) > 1 else None
    transfer_ok = (
        successes == rounds
        and downloaded_bytes == rounds * LOCAL_PROXY_DEEP_DOWNLOAD_BYTES
        and uploaded_bytes == rounds * LOCAL_PROXY_DEEP_UPLOAD_BYTES
    )
    compact = CodexCompactTransportProbeResult(
        ok=False,
        successes=0,
        attempts=LOCAL_CODEX_COMPACT_PROBE_ATTEMPTS,
        detail="大文件传输不完整，未执行 compact 路径预检",
    )
    if transfer_ok and not _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
        try:
            compact = compact_probe(
                proxy_url,
                timeout=timeout,
                attempts=LOCAL_CODEX_COMPACT_PROBE_ATTEMPTS,
                payload_bytes=LOCAL_CODEX_COMPACT_PROBE_PAYLOAD_BYTES,
            )
        except Exception as exc:
            compact = CodexCompactTransportProbeResult(
                ok=False,
                successes=0,
                attempts=LOCAL_CODEX_COMPACT_PROBE_ATTEMPTS,
                detail=str(exc).splitlines()[0][:200] or type(exc).__name__,
            )

    failed_details = [result.detail for result in transfer_results if not result.ok and result.detail]
    if transfer_ok and compact.ok:
        detail = (
            f"Codex 长会话网络近似通过：{rounds}/{rounds} 轮 "
            f"1MiB 下载 + 512KiB 上传完整，传输中位 {median_ms}ms；"
            f"{compact.detail}"
        )
    elif not transfer_ok:
        detail = (
            f"Codex 长会话网络近似失败：完整传输 {successes}/{rounds} 轮，"
            f"下载 {downloaded_bytes}/{rounds * LOCAL_PROXY_DEEP_DOWNLOAD_BYTES} 字节，"
            f"上传 {uploaded_bytes}/{rounds * LOCAL_PROXY_DEEP_UPLOAD_BYTES} 字节"
        )
        if failed_details:
            detail += "；" + "；".join(failed_details[:2])
    else:
        detail = (
            f"{rounds}/{rounds} 轮大文件传输完整，但官方 compact 路径预检失败；"
            f"{compact.detail}"
        )
    return LocalProxyDeepTransportProbeResult(
        ok=bool(transfer_ok and compact.ok),
        transfer_ok=transfer_ok,
        transfer_successes=successes,
        transfer_attempts=rounds,
        downloaded_bytes=downloaded_bytes,
        uploaded_bytes=uploaded_bytes,
        median_ms=median_ms,
        jitter_ms=jitter_ms,
        codex_compact_ok=compact.ok,
        codex_compact_elapsed_ms=compact.median_ms,
        codex_compact_detail=compact.detail,
        detail=detail[:900],
    )


def _probe_cloudflare_transfer_round(
    proxy_url: str,
    *,
    timeout: int = 8,
) -> LocalProxyTransferRoundResult:
    started = time.monotonic()
    if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
        return LocalProxyTransferRoundResult(
            ok=False,
            detail="应用正在退出，已取消深度传输验证",
        )
    download_ok, downloaded, download_detail = _download_exact_bytes_through_explicit_http_proxy(
        proxy_url,
        LOCAL_PROXY_DEEP_DOWNLOAD_URL,
        LOCAL_PROXY_DEEP_DOWNLOAD_BYTES,
        timeout=timeout,
    )
    if not download_ok:
        return LocalProxyTransferRoundResult(
            ok=False,
            downloaded_bytes=downloaded,
            elapsed_ms=_elapsed_ms(started),
            detail=f"下载不完整: {download_detail}",
        )
    upload_ok, uploaded, upload_detail = _upload_exact_bytes_through_explicit_http_proxy(
        proxy_url,
        LOCAL_PROXY_DEEP_UPLOAD_URL,
        LOCAL_PROXY_DEEP_UPLOAD_BYTES,
        timeout=timeout,
    )
    return LocalProxyTransferRoundResult(
        ok=bool(download_ok and upload_ok),
        downloaded_bytes=downloaded,
        uploaded_bytes=uploaded,
        elapsed_ms=_elapsed_ms(started),
        detail=(
            f"下载 {downloaded} 字节；{upload_detail}"
            if upload_ok
            else f"上传不完整: {upload_detail}"
        ),
    )


@contextmanager
def _isolated_mihomo_session(binary_path: Path, proxy_node: dict):
    """Run one node in a disposable mihomo instance without managed state."""

    process = None
    probe_dir = Path(tempfile.mkdtemp(prefix="api-switcher-node-probe-"))
    with _ISOLATED_MIHOMO_LOCK:
        _ISOLATED_MIHOMO_DIRECTORIES.add(probe_dir)
    try:
        if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
            raise RuntimeError("应用正在退出，已取消节点稳定验证")
        mixed_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            mixed_socket.bind(("127.0.0.1", 0))
            mixed_port = int(mixed_socket.getsockname()[1])
        finally:
            mixed_socket.close()

        config = _build_isolated_mihomo_probe_config(proxy_node, mixed_port)
        config_path = probe_dir / "config.yaml"
        atomic_write_text(config_path, config)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with _ISOLATED_MIHOMO_LOCK:
            if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
                raise RuntimeError("应用正在退出，已取消节点稳定验证")
            process = subprocess.Popen(
                [str(binary_path), "-d", str(probe_dir)],
                cwd=str(probe_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            _ISOLATED_MIHOMO_PROCESSES.add(process)
        try:
            for _attempt in range(30):
                if process.poll() is not None:
                    raise RuntimeError("临时 mihomo 启动失败")
                if _is_port_listening(mixed_port):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("临时 mihomo 启动后未监听")
            yield _IsolatedMihomoSession(
                proxy_url=_proxy_url(mixed_port),
                config_path=config_path,
                mixed_port=mixed_port,
                process=process,
            )
        finally:
            if not _terminate_isolated_mihomo_process(process):
                raise RuntimeError("临时 mihomo 无法确认已退出，凭据目录暂未删除")
    finally:
        with _ISOLATED_MIHOMO_LOCK:
            if process is not None and process.poll() is not None:
                _ISOLATED_MIHOMO_PROCESSES.discard(process)
        if process is None or process.poll() is not None:
            shutil.rmtree(probe_dir, ignore_errors=True)
            with _ISOLATED_MIHOMO_LOCK:
                _ISOLATED_MIHOMO_DIRECTORIES.discard(probe_dir)


def _build_isolated_mihomo_probe_config(proxy_node: dict, mixed_port: int) -> str:
    node = remote_proxy._normalize_proxy_node(proxy_node)
    proxy_name = str(node.get("name") or "AI-PROBE-NODE")
    group_name = "API-SWITCHER-PROBE-GROUP"
    if proxy_name == group_name:
        proxy_name = "API-SWITCHER-PROBE-NODE"
    node["name"] = proxy_name
    config = {
        "mixed-port": int(mixed_port),
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "silent",
        "ipv6": True,
        "proxies": [node],
        "proxy-groups": [{"name": group_name, "type": "select", "proxies": [proxy_name]}],
        "rules": [f"MATCH,{group_name}"],
    }
    return remote_proxy.AI_PROXY_CONFIG_MARKER + " isolated\n" + remote_proxy._dump_yaml(config)


def _terminate_isolated_mihomo_process(process, *, timeout_seconds: float = 2.0) -> bool:
    if process is None or process.poll() is not None:
        return True
    try:
        process.terminate()
        process.wait(timeout=max(0.1, float(timeout_seconds)))
    except Exception:
        try:
            process.kill()
            process.wait(timeout=max(0.1, min(1.0, float(timeout_seconds))))
        except Exception:
            return False
    return process.poll() is not None


def cleanup_isolated_mihomo_sessions_for_shutdown() -> None:
    """Cancel future probe candidates and synchronously reap disposable mihomo."""

    with _ISOLATED_MIHOMO_LOCK:
        _ISOLATED_MIHOMO_SHUTTING_DOWN.set()
        processes = tuple(_ISOLATED_MIHOMO_PROCESSES)
        directories = tuple(_ISOLATED_MIHOMO_DIRECTORIES)
    for process in processes:
        _terminate_isolated_mihomo_process(process, timeout_seconds=1.5)
    for directory in directories:
        if not any(
            process.poll() is None
            for process in processes
            if process in _ISOLATED_MIHOMO_PROCESSES
        ):
            shutil.rmtree(directory, ignore_errors=True)
    with _ISOLATED_MIHOMO_LOCK:
        stopped = {process for process in _ISOLATED_MIHOMO_PROCESSES if process.poll() is not None}
        _ISOLATED_MIHOMO_PROCESSES.difference_update(stopped)
        if not _ISOLATED_MIHOMO_PROCESSES:
            _ISOLATED_MIHOMO_DIRECTORIES.clear()


atexit.register(cleanup_isolated_mihomo_sessions_for_shutdown)


def _ensure_local_dirs() -> None:
    LOCAL_PROXY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_PROXY_BIN_DIR.mkdir(parents=True, exist_ok=True)


def _local_proxy_preferences_signature() -> tuple[str, int | None, int | None]:
    path_key = str(LOCAL_PROXY_PREFS_PATH.resolve(strict=False))
    try:
        stat = LOCAL_PROXY_PREFS_PATH.stat()
        return (path_key, int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (path_key, None, None)


def _cache_local_proxy_preferences(
    preferences: dict,
    signature: tuple[str, int | None, int | None] | None = None,
) -> None:
    global _LOCAL_PROXY_PREFS_CACHE, _LOCAL_PROXY_PREFS_CACHE_SIGNATURE
    _LOCAL_PROXY_PREFS_CACHE = copy.deepcopy(preferences)
    _LOCAL_PROXY_PREFS_CACHE_SIGNATURE = signature or _local_proxy_preferences_signature()


def _local_proxy_state_signature() -> tuple[str, int | None, int | None, str]:
    path_key = str(LOCAL_PROXY_STATE_PATH.resolve(strict=False))
    try:
        stat = LOCAL_PROXY_STATE_PATH.stat()
        digest = hashlib.sha256(LOCAL_PROXY_STATE_PATH.read_bytes()).hexdigest()
        return (path_key, int(stat.st_mtime_ns), int(stat.st_size), digest)
    except OSError:
        return (path_key, None, None, "")


def _cache_local_proxy_state(
    state: dict,
    signature: tuple[str, int | None, int | None, str] | None = None,
) -> None:
    global _LOCAL_PROXY_STATE_CACHE, _LOCAL_PROXY_STATE_CACHE_SIGNATURE
    _LOCAL_PROXY_STATE_CACHE = copy.deepcopy(state)
    _LOCAL_PROXY_STATE_CACHE_SIGNATURE = signature or _local_proxy_state_signature()


def _normalize_local_proxy_preferences(data: dict | None) -> dict:
    raw = data if isinstance(data, dict) else {}
    builtin_sites = {}
    raw_sites = raw.get("builtin_sites")
    if isinstance(raw_sites, dict):
        for site_id, enabled in raw_sites.items():
            site_key = str(site_id or "").strip()
            if site_key in LOCAL_PROXY_BUILTIN_SITE_IDS:
                builtin_sites[site_key] = _coerce_bool(enabled)

    custom_targets = []
    seen_custom = set()
    raw_targets = raw.get("custom_targets")
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            try:
                normalized = normalize_proxy_target(str(item.get("value") or item.get("target") or ""))
            except ValueError:
                continue
            key = (normalized["kind"], normalized["value"].casefold())
            if key in seen_custom:
                continue
            seen_custom.add(key)
            custom_targets.append({
                "id": str(item.get("id") or uuid.uuid4().hex),
                "target": normalized["target"],
                "kind": normalized["kind"],
                "value": normalized["value"],
                "enabled": _coerce_bool(item.get("enabled"), True),
                "created_at": str(item.get("created_at") or remote_proxy._now_iso()),
            })

    last_node = raw.get("last_node") if isinstance(raw.get("last_node"), dict) else None
    if last_node:
        try:
            last_node = remote_proxy._normalize_proxy_node(last_node)
        except Exception:
            last_node = None

    return {
        "start_on_login": _coerce_bool(raw.get("start_on_login")),
        "keep_running_on_exit": _coerce_bool(raw.get("keep_running_on_exit"), True),
        "proxy_non_cn": _coerce_bool(raw.get("proxy_non_cn")),
        "strict_privacy": _coerce_bool(raw.get("strict_privacy")),
        "builtin_sites": builtin_sites,
        "custom_targets": custom_targets,
        "last_node": last_node,
        "updated_at": str(raw.get("updated_at") or ""),
    }


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _extract_host_from_target(raw: str) -> str:
    text = raw.strip()
    parsed = urlparse(text if "://" in text else f"//{text}", scheme="https")
    host = parsed.hostname
    if host:
        return host
    host_part = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if host_part.count(":") == 1:
        host_part = host_part.rsplit(":", 1)[0]
    return host_part.strip("[]")


def _normalize_domain_target(host: str) -> str:
    domain = str(host or "").strip().strip(".").lower()
    if domain.startswith("*."):
        domain = domain[2:]
    if not domain:
        raise ValueError("没有识别到有效网址")
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("网址包含无法识别的字符") from exc
    if not LOCAL_PROXY_DOMAIN_PATTERN.match(domain):
        raise ValueError("网址格式不正确，请输入类似 youtube.com 或 https://youtube.com")
    return domain


def _routing_options_from_preferences(preferences: dict | None = None) -> dict:
    if preferences is None:
        preferences = _load_local_proxy_routing_preferences_strict()
    domains = []
    ip_cidrs = []
    builtin_sites = preferences.get("builtin_sites") if isinstance(preferences.get("builtin_sites"), dict) else {}
    for site in LOCAL_PROXY_BUILTIN_SITES:
        if builtin_sites.get(str(site["id"])):
            domains.extend(str(target) for target in site.get("targets") or ())
    for entry in preferences.get("custom_targets") or []:
        if not isinstance(entry, dict) or not _coerce_bool(entry.get("enabled"), True):
            continue
        if entry.get("kind") == "ip-cidr":
            ip_cidrs.append(str(entry.get("value") or ""))
        elif entry.get("kind") == "domain":
            domains.append(str(entry.get("value") or ""))
    return {
        "extra_proxy_domains": tuple(domains),
        "extra_proxy_ip_cidrs": tuple(ip_cidrs),
        "proxy_non_cn": _coerce_bool(preferences.get("proxy_non_cn")),
        "strict_privacy": _coerce_bool(preferences.get("strict_privacy")),
    }


def _build_local_mihomo_config(
    proxy_node: dict,
    mixed_port: int,
    *,
    preferences: dict | None = None,
) -> str:
    return remote_proxy.build_mihomo_config(
        proxy_node,
        mixed_port,
        **_routing_options_from_preferences(preferences),
    )


def _save_last_proxy_node(proxy_node: dict) -> None:
    try:
        _save_last_proxy_node_strict(proxy_node)
    except Exception:
        return


def _save_last_proxy_node_strict(proxy_node: dict) -> None:
    normalized = remote_proxy._normalize_proxy_node(proxy_node)
    save_local_proxy_preferences(last_node=normalized)


def _load_last_proxy_node() -> dict | None:
    node = load_local_proxy_preferences().get("last_node")
    if not isinstance(node, dict):
        return None
    try:
        return remote_proxy._normalize_proxy_node(node)
    except Exception:
        return None


def _remember_selected_subscription_node(proxy_node: dict, *, profile_id: str = "") -> None:
    try:
        if profile_id:
            remote_proxy.set_proxy_subscription_selected_node(proxy_node, profile_id=profile_id)
        else:
            remote_proxy.set_proxy_subscription_selected_node(proxy_node)
    except Exception:
        return


def _local_config_sha256(config_path: Path) -> str:
    try:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _clear_applied_local_config(state: dict) -> None:
    for key in LOCAL_PROXY_APPLIED_CONFIG_STATE_KEYS:
        state.pop(key, None)


def _stamp_applied_local_config(
    state: dict,
    config_path: Path,
    mixed_port: int,
    *,
    pid: int | None,
    config_sha256: str = "",
) -> None:
    """Mark a config as successfully loaded by this exact managed process."""

    try:
        normalized_pid = int(pid or 0)
    except (TypeError, ValueError):
        normalized_pid = 0
    normalized_port = remote_proxy._normalize_port(mixed_port, "本机代理端口")
    fingerprint = str(config_sha256 or _local_config_sha256(config_path)).strip().lower()
    if normalized_pid <= 0:
        raise RuntimeError("未读取到已加载配置的受管代理 PID")
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise RuntimeError("无法计算已加载配置的 SHA-256 指纹")
    state.update(
        {
            "applied_config_sha256": fingerprint,
            "applied_config_pid": normalized_pid,
            "applied_config_mixed_port": normalized_port,
            "applied_config_at": remote_proxy._now_iso(),
        }
    )


def _applied_local_config_matches(
    state: dict,
    config_path: Path,
    mixed_port: int,
    *,
    pid: int | None,
) -> bool:
    """Trust only a fingerprint recorded after start/controller success."""

    fingerprint = str(state.get("applied_config_sha256") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        return False
    try:
        applied_pid = int(state.get("applied_config_pid") or 0)
        applied_port = int(state.get("applied_config_mixed_port") or 0)
        current_pid = int(pid or 0)
        current_port = int(mixed_port)
    except (TypeError, ValueError):
        return False
    if applied_pid <= 0 or applied_pid != current_pid or applied_port != current_port:
        return False
    current_fingerprint = _local_config_sha256(config_path)
    return bool(current_fingerprint and current_fingerprint == fingerprint)


def _load_state() -> dict:
    with _LOCAL_PROXY_STATE_LOCK:
        signature = _local_proxy_state_signature()
        if _LOCAL_PROXY_STATE_CACHE is not None and _LOCAL_PROXY_STATE_CACHE_SIGNATURE == signature:
            return copy.deepcopy(_LOCAL_PROXY_STATE_CACHE)
        try:
            data = json.loads(LOCAL_PROXY_STATE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            _quarantine_corrupt_local_json_file(LOCAL_PROXY_STATE_PATH)
            data = {}
            signature = _local_proxy_state_signature()
        except Exception:
            data = {}
        state = data if isinstance(data, dict) else {}
        _cache_local_proxy_state(state, signature)
        return copy.deepcopy(state)


def _save_state(state: dict) -> None:
    with _LOCAL_PROXY_STATE_LOCK:
        _ensure_local_dirs()
        atomic_write_text(
            LOCAL_PROXY_STATE_PATH,
            json.dumps(state, ensure_ascii=False, indent=2),
        )
        _cache_local_proxy_state(state if isinstance(state, dict) else {})


def _quarantine_corrupt_local_json_file(path: Path) -> Path | None:
    try:
        if not path.exists():
            return None
        timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        target = path.with_name(f"{path.name}.corrupt-{timestamp}-{uuid.uuid4().hex[:8]}")
        path.replace(target)
        return target
    except OSError:
        return None


def _proxy_url(mixed_port: int) -> str:
    return f"http://127.0.0.1:{mixed_port}"


def _select_local_mixed_port(preferred_port: int = DEFAULT_LOCAL_MIXED_PORT) -> int:
    state = _load_state()
    pid = _read_pid()
    preferred = remote_proxy._normalize_port(
        state.get("mixed_port") or preferred_port,
        "本机代理端口",
    )
    if pid and _is_pid_running(pid) and _is_managed_mihomo_pid(pid, state=state):
        return preferred
    if not _is_port_listening(preferred):
        return preferred
    for port in LOCAL_PORT_CANDIDATES:
        if not _is_port_listening(port):
            return port
    raise RuntimeError(
        f"本机 AI 代理候选端口 {LOCAL_PORT_CANDIDATES[0]}-{LOCAL_PORT_CANDIDATES[-1]} 均被占用"
    )


def _local_proxy_env_values(mixed_port: int) -> dict[str, str]:
    return remote_proxy._proxy_env_values(mixed_port)


def _apply_local_env(mixed_port: int) -> None:
    persistent_env.set_local_user_env(_local_proxy_env_values(mixed_port))


def _capture_previous_env() -> dict:
    previous = {}
    for key in remote_proxy.PROXY_ENV_KEYS:
        value = persistent_env._local_user_env_value(key)
        previous[key] = {"exists": value is not None, "value": value or ""}
    return previous


def _restore_local_env(state: dict, mixed_port: int) -> None:
    previous = state.get("previous_env")
    if not isinstance(previous, dict):
        return
    expected = _local_proxy_env_values(mixed_port)
    updates = {}
    deletes = []
    for key in remote_proxy.PROXY_ENV_KEYS:
        current = persistent_env._local_user_env_value(key)
        if current != expected.get(key):
            continue
        item = previous.get(key)
        if isinstance(item, dict) and item.get("exists") and item.get("value"):
            updates[key] = str(item.get("value") or "")
        else:
            deletes.append(key)
    if updates:
        persistent_env.set_local_user_env(updates)
    if deletes:
        persistent_env.delete_local_user_env(deletes)


def _restore_managed_settings(state: dict, mixed_port: int) -> list[str]:
    errors = []
    try:
        _restore_local_env(state, mixed_port)
    except Exception as exc:
        errors.append(f"Windows 环境变量: {exc}")
    try:
        _restore_local_vscode_proxy(state, mixed_port)
    except Exception as exc:
        errors.append(f"VS Code 设置: {exc}")
    try:
        _restore_windows_system_proxy(state, mixed_port)
    except Exception as exc:
        errors.append(f"Windows 系统代理: {exc}")
    return errors


def _save_restore_retry_state(state: dict, mixed_port: int, restore_errors: list[str]) -> None:
    if not restore_errors:
        _save_state({})
        return
    retry_state = dict(state or {})
    retry_state.pop("pid", None)
    retry_state["mixed_port"] = mixed_port
    retry_state["last_restore_error"] = "; ".join(str(item) for item in restore_errors)
    retry_state["updated_at"] = remote_proxy._now_iso()
    _save_state(retry_state)


def _local_env_matches(mixed_port: int) -> bool:
    if os.name != "nt":
        return False
    expected = _local_proxy_env_values(mixed_port)
    for key in remote_proxy.PROXY_ENV_KEYS:
        if persistent_env._local_user_env_value(key) != expected.get(key):
            return False
    return True


def _windows_system_proxy_expected_values(mixed_port: int) -> dict[str, object]:
    mixed_port = remote_proxy._normalize_port(mixed_port, "本机代理端口")
    return {
        "ProxyEnable": 1,
        "ProxyServer": f"127.0.0.1:{mixed_port}",
        "ProxyOverride": WINDOWS_SYSTEM_PROXY_OVERRIDE,
        "AutoConfigURL": "",
        "AutoDetect": 0,
    }


def _windows_system_proxy_matches_values(values: dict, mixed_port: int) -> bool:
    expected = _windows_system_proxy_expected_values(mixed_port)
    return (
        int(values.get("ProxyEnable") or 0) == expected["ProxyEnable"]
        and str(values.get("ProxyServer") or "") == expected["ProxyServer"]
        and str(values.get("ProxyOverride") or "") == expected["ProxyOverride"]
        and str(values.get("AutoConfigURL") or "") == expected["AutoConfigURL"]
        and int(values.get("AutoDetect") or 0) == expected["AutoDetect"]
    )


def _windows_system_proxy_matches(mixed_port: int) -> bool:
    if os.name != "nt":
        return False
    return _windows_system_proxy_matches_values(_read_windows_system_proxy_values(), mixed_port)


def _capture_windows_system_proxy_state() -> dict:
    if os.name != "nt":
        return {}
    previous = {}
    for name in WINDOWS_SYSTEM_PROXY_KEYS:
        exists, value, value_type = _read_windows_system_proxy_value(name)
        previous[name] = {"exists": exists, "value": value, "type": value_type}
    return previous


def _read_windows_system_proxy_value(name: str) -> tuple[bool, object, int | None]:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_SYSTEM_PROXY_REG_PATH, 0, winreg.KEY_READ) as key:
            value, value_type = winreg.QueryValueEx(key, name)
            return True, value, value_type
    except FileNotFoundError:
        return False, "", None


def _read_windows_system_proxy_values() -> dict[str, object]:
    values = {}
    for name in WINDOWS_SYSTEM_PROXY_KEYS:
        exists, value, _value_type = _read_windows_system_proxy_value(name)
        if exists:
            values[name] = value
    return values


def _apply_windows_system_proxy(mixed_port: int) -> None:
    if os.name != "nt":
        return
    import winreg

    expected = _windows_system_proxy_expected_values(mixed_port)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WINDOWS_SYSTEM_PROXY_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, int(expected["ProxyEnable"]))
        winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, str(expected["ProxyServer"]))
        winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, str(expected["ProxyOverride"]))
        winreg.SetValueEx(key, "AutoDetect", 0, winreg.REG_DWORD, int(expected["AutoDetect"]))
        try:
            winreg.DeleteValue(key, "AutoConfigURL")
        except FileNotFoundError:
            pass
    _notify_windows_proxy_change()


def _restore_windows_system_proxy(state: dict, mixed_port: int) -> None:
    if os.name != "nt":
        return
    previous = state.get("previous_system_proxy")
    if not isinstance(previous, dict):
        return
    if not _windows_system_proxy_matches(mixed_port):
        return

    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, WINDOWS_SYSTEM_PROXY_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
        for name in WINDOWS_SYSTEM_PROXY_KEYS:
            item = previous.get(name)
            if isinstance(item, dict) and item.get("exists"):
                value = item.get("value")
                value_type = item.get("type") or (winreg.REG_DWORD if name in {"ProxyEnable", "AutoDetect"} else winreg.REG_SZ)
                if name in {"ProxyEnable", "AutoDetect"}:
                    value = int(value or 0)
                else:
                    value = str(value or "")
                winreg.SetValueEx(key, name, 0, value_type, value)
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    _notify_windows_proxy_change()


def _notify_windows_proxy_change() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        internet_option_refresh = 37
        internet_option_settings_changed = 39
        ctypes.windll.Wininet.InternetSetOptionW(0, internet_option_settings_changed, 0, 0)
        ctypes.windll.Wininet.InternetSetOptionW(0, internet_option_refresh, 0, 0)
    except Exception:
        return


def _reload_local_mihomo_config(config_path: Path, mixed_port: int) -> None:
    controller_port = remote_proxy.mihomo_controller_port(mixed_port)
    payload = json.dumps({"path": str(config_path)}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{controller_port}/configs?force=true",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        status = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
        if status < 200 or status >= 300:
            raise RuntimeError(f"mihomo reload HTTP {status}")


def _restore_local_proxy_node_after_failed_update(
    original_node: dict | None,
    attempted_node: dict | None,
    *,
    profile_id: str = "",
) -> str:
    if not original_node:
        return "；未读取到更新前节点，已保留最后一次热更新状态"
    try:
        original = remote_proxy._normalize_proxy_node(original_node)
    except Exception:
        return "；更新前节点格式不可恢复，已保留最后一次热更新状态"
    restore_message = ""
    try:
        if profile_id:
            restore_message = reload_local_ai_proxy(
                remote_proxy.format_proxy_node(original),
                profile_id=profile_id,
            )
        else:
            restore_message = reload_local_ai_proxy(remote_proxy.format_proxy_node(original))
    except Exception as exc:
        attempted = remote_proxy.describe_proxy_node(attempted_node or {}) if attempted_node else "当前节点"
        return f"；尝试从 {attempted} 恢复更新前节点失败: {exc}"
    if any(marker in restore_message for marker in ("跳过", "未运行", "不是本工具受管")):
        return f"；恢复更新前节点未执行成功: {restore_message}"
    try:
        restore_probe = probe_local_ai_proxy()
    except Exception as exc:
        return f"；已恢复更新前节点 {remote_proxy.describe_proxy_node(original)}，但恢复后验证失败: {exc}"
    if remote_proxy._probe_summary_all_ok(restore_probe):
        return f"；已恢复更新前节点 {remote_proxy.describe_proxy_node(original)}，验证通过: {remote_proxy._compact_probe_summary(restore_probe)}"
    return (
        f"；已恢复更新前节点 {remote_proxy.describe_proxy_node(original)}，"
        f"但验证仍未完全通过: {remote_proxy._compact_probe_summary(restore_probe)}"
    )


def _read_local_managed_proxy_node() -> dict | None:
    state = _load_state()
    config_path = Path(state.get("config_path") or (LOCAL_PROXY_CONFIG_DIR / "config.yaml"))
    try:
        content = config_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    if remote_proxy.AI_PROXY_CONFIG_MARKER not in content:
        return None
    try:
        parsed = remote_proxy.yaml.safe_load(content)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    proxies = parsed.get("proxies")
    if not isinstance(proxies, list) or not proxies:
        return None
    node = proxies[0]
    if not isinstance(node, dict):
        return None
    try:
        normalized = remote_proxy._normalize_proxy_node(node)
    except Exception:
        return None
    display_name = remote_proxy._managed_proxy_display_name(content)
    if display_name:
        normalized["name"] = display_name
    return normalized


def _capture_vscode_proxy_state(settings: dict) -> dict:
    terminal_env = settings.get("terminal.integrated.env.windows")
    if not isinstance(terminal_env, dict):
        terminal_env = {}
    return {
        "http.proxy": {"exists": "http.proxy" in settings, "value": settings.get("http.proxy")},
        "http.proxySupport": {
            "exists": "http.proxySupport" in settings,
            "value": settings.get("http.proxySupport"),
        },
        "terminal.integrated.env.windows": {
            key: {"exists": key in terminal_env, "value": terminal_env.get(key)}
            for key in remote_proxy.PROXY_ENV_KEYS
        },
    }


def _apply_local_vscode_proxy_settings(settings: dict, mixed_port: int) -> tuple[dict, bool]:
    env = _local_proxy_env_values(mixed_port)
    updated = dict(settings or {})
    changed = False
    proxy_url = env["API_SWITCHER_AI_PROXY_URL"]

    if updated.get("http.proxy") != proxy_url:
        updated["http.proxy"] = proxy_url
        changed = True
    if updated.get("http.proxySupport") != "override":
        updated["http.proxySupport"] = "override"
        changed = True

    terminal_env = updated.get("terminal.integrated.env.windows")
    if not isinstance(terminal_env, dict):
        terminal_env = {}
    else:
        terminal_env = dict(terminal_env)
    for key in remote_proxy.PROXY_ENV_KEYS:
        if terminal_env.get(key) != env[key]:
            terminal_env[key] = env[key]
            changed = True
    if updated.get("terminal.integrated.env.windows") != terminal_env:
        updated["terminal.integrated.env.windows"] = terminal_env
        changed = True
    return updated, changed


def _apply_local_vscode_proxy(mixed_port: int) -> None:
    settings = vscode_parser.read_vscode_settings()
    updated, changed = _apply_local_vscode_proxy_settings(settings, mixed_port)
    if changed:
        vscode_parser.write_vscode_settings(updated)


def _restore_vscode_key(settings: dict, key: str, previous: dict, expected_value) -> bool:
    item = previous.get(key)
    if not isinstance(item, dict) or settings.get(key) != expected_value:
        return False
    if item.get("exists"):
        settings[key] = item.get("value")
    else:
        settings.pop(key, None)
    return True


def _restore_vscode_proxy_settings(settings: dict, previous: dict, mixed_port: int) -> tuple[dict, bool]:
    env = _local_proxy_env_values(mixed_port)
    updated = dict(settings or {})
    changed = False
    changed = _restore_vscode_key(updated, "http.proxy", previous, env["API_SWITCHER_AI_PROXY_URL"]) or changed
    changed = _restore_vscode_key(updated, "http.proxySupport", previous, "override") or changed

    terminal_key = "terminal.integrated.env.windows"
    terminal_env = updated.get(terminal_key)
    if not isinstance(terminal_env, dict):
        terminal_env = {}
    else:
        terminal_env = dict(terminal_env)
    previous_terminal = previous.get(terminal_key)
    if isinstance(previous_terminal, dict):
        for key in remote_proxy.PROXY_ENV_KEYS:
            if terminal_env.get(key) != env[key]:
                continue
            item = previous_terminal.get(key)
            if isinstance(item, dict) and item.get("exists"):
                terminal_env[key] = item.get("value")
            else:
                terminal_env.pop(key, None)
            changed = True
    if changed:
        if terminal_env:
            updated[terminal_key] = terminal_env
        else:
            updated.pop(terminal_key, None)
    return updated, changed


def _restore_local_vscode_proxy(state: dict, mixed_port: int) -> None:
    previous = state.get("previous_vscode")
    if not isinstance(previous, dict):
        return
    settings = vscode_parser.read_vscode_settings()
    updated, changed = _restore_vscode_proxy_settings(settings, previous, mixed_port)
    if changed:
        vscode_parser.write_vscode_settings(updated)


def _local_vscode_proxy_matches(settings: dict, mixed_port: int) -> bool:
    _updated, changed = _apply_local_vscode_proxy_settings(settings, mixed_port)
    return not changed


def _local_vscode_proxy_match_detail(mixed_port: int) -> str:
    try:
        settings = vscode_parser.read_vscode_settings()
    except Exception as exc:
        return f"VS Code 设置读取失败: {exc}"
    return "VS Code 本机设置已指向本机代理" if _local_vscode_proxy_matches(settings, mixed_port) else "VS Code 本机设置未完全指向本机代理"


def _ensure_mihomo_binary() -> Path:
    binary_path = LOCAL_PROXY_BIN_DIR / "mihomo.exe"
    if binary_path.exists():
        return binary_path

    existing = shutil.which("mihomo") or shutil.which("mihomo.exe") or shutil.which("clash") or shutil.which("clash.exe")
    if existing:
        return Path(existing)

    _download_mihomo_binary(binary_path)
    return binary_path


def _windows_asset_pattern() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "windows-arm64"
    if machine in {"amd64", "x86_64", "x64"}:
        return "windows-amd64"
    raise RuntimeError(f"不支持的 Windows 架构: {platform.machine()}")


def _pick_mihomo_asset(assets: list[dict], pattern: str) -> dict:
    def usable(asset: dict) -> bool:
        name = str(asset.get("name") or "").lower()
        if pattern not in name:
            return False
        if not name.endswith((".zip", ".gz", ".exe")):
            return False
        return not any(token in name for token in ("sha256", "checksums"))

    candidates = [asset for asset in assets if usable(asset) and "compatible" not in str(asset.get("name", "")).lower()]
    if not candidates:
        candidates = [asset for asset in assets if usable(asset)]
    if not candidates:
        raise RuntimeError(f"没有找到匹配 {pattern} 的 mihomo Windows 发行包")
    return candidates[0]


def _download_mihomo_binary(target: Path) -> None:
    pattern = _windows_asset_pattern()
    release_request = urllib.request.Request(
        "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "API-Switcher/1.0",
        },
    )
    data = json.loads(_read_url_with_retries(release_request, timeout=45, label="读取 mihomo 最新版本").decode("utf-8"))
    asset = _pick_mihomo_asset(data.get("assets") or [], pattern)
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise RuntimeError("mihomo 发行包缺少下载地址")
    asset_request = urllib.request.Request(url, headers={"User-Agent": "API-Switcher/1.0"})
    payload = _read_url_with_retries(asset_request, timeout=180, label="下载 mihomo Windows 发行包")
    _write_mihomo_payload(target, url, payload)


def _read_url_with_retries(
    request: urllib.request.Request,
    *,
    timeout: int,
    label: str,
    retries: int = MIHOMO_DOWNLOAD_RETRIES,
) -> bytes:
    try:
        attempts = max(1, int(retries))
    except (TypeError, ValueError):
        attempts = MIHOMO_DOWNLOAD_RETRIES
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
        if attempt < attempts:
            delay = remote_proxy._retry_delay_seconds(1.0, attempt)
            if delay > 0:
                time.sleep(delay)
    suffix = f"（已重试 {attempts} 次）" if attempts > 1 else ""
    raise RuntimeError(f"{label}失败{suffix}: {last_error}") from last_error


def _probe_url_through_proxy(proxy_url: str, label: str, url: str, timeout: int = 8) -> LocalAIProxyProbeResult:
    # urllib's ProxyHandler still consults NO_PROXY.  The managed proxy probe
    # must never turn into a direct request merely because the parent process
    # carries NO_PROXY='*' or an AI hostname override.
    return _probe_ai_url_through_explicit_http_proxy(proxy_url, label, url, timeout)


def _probe_ai_url_through_explicit_http_proxy(
    proxy_url: str,
    label: str,
    url: str,
    timeout: int = 8,
) -> LocalAIProxyProbeResult:
    """Probe an HTTPS target through an explicit CONNECT tunnel.

    This deliberately avoids urllib's environment-aware proxy bypass logic, so
    HTTP(S)_PROXY and NO_PROXY cannot silently turn a node test into a direct
    request from the host.
    """

    started = time.monotonic()
    connection = None
    try:
        proxy = urlparse(proxy_url)
        target = urlparse(url)
        if proxy.scheme.lower() != "http" or not proxy.hostname or not proxy.port:
            raise ValueError("临时代理地址必须是带端口的 HTTP 地址")
        if target.scheme.lower() != "https" or not target.hostname:
            raise ValueError("AI 探测目标必须是 HTTPS 地址")
        target_port = int(target.port or 443)
        target_path = target.path or "/"
        if target.query:
            target_path += "?" + target.query

        connection = http.client.HTTPSConnection(
            proxy.hostname,
            int(proxy.port),
            timeout=max(1, min(60, int(timeout or 8))),
            context=ssl.create_default_context(),
        )
        connection.set_tunnel(target.hostname, target_port)
        host_header = target.hostname if target_port == 443 else f"{target.hostname}:{target_port}"
        connection.request(
            "GET",
            target_path,
            headers={
                "Host": host_header,
                "Accept": "application/json,text/plain,*/*",
                "User-Agent": "API-Switcher/1.0",
            },
        )
        response = connection.getresponse()
        status = int(response.status or 0)
        payload = response.read(64 * 1024)
        body = payload.decode("utf-8", errors="replace")
        headers = {str(key).lower(): str(value) for key, value in response.getheaders()}
        ok, exit_country, detail = _classify_ai_probe_response(label, status, body, headers)
        return LocalAIProxyProbeResult(
            label=label,
            ok=ok,
            status=status,
            detail=detail,
            elapsed_ms=_elapsed_ms(started),
            exit_country=exit_country,
        )
    except Exception as exc:
        return LocalAIProxyProbeResult(
            label=label,
            ok=False,
            detail=str(exc).splitlines()[0][:160] or type(exc).__name__,
            elapsed_ms=_elapsed_ms(started),
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _post_unauthenticated_json_through_explicit_http_proxy(
    proxy_url: str,
    url: str,
    payload: bytes,
    *,
    timeout: int = 8,
) -> LocalAIProxyProbeResult:
    """POST fixed JSON through the isolated proxy without credentials."""

    started = time.monotonic()
    deadline = started + LOCAL_PROXY_DEEP_OPERATION_DEADLINE_SECONDS
    connection = None
    try:
        initial_timeout = _prepare_deep_probe_io(
            None,
            deadline,
            timeout,
            "compact 路径连接",
        )
        connection, _target, target_path, host_header = _open_explicit_https_proxy_connection(
            proxy_url,
            url,
            initial_timeout,
        )
        _prepare_deep_probe_io(connection, deadline, timeout, "compact 请求头")
        connection.putrequest(
            "POST",
            target_path,
            skip_host=True,
            skip_accept_encoding=True,
        )
        connection.putheader("Host", host_header)
        connection.putheader("Accept", "application/json")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(payload)))
        connection.putheader("User-Agent", "API-Switcher/1.0")
        connection.endheaders()
        for offset in range(0, len(payload), LOCAL_PROXY_DEEP_IO_CHUNK_BYTES):
            _prepare_deep_probe_io(connection, deadline, timeout, "compact 请求体")
            connection.send(payload[offset : offset + LOCAL_PROXY_DEEP_IO_CHUNK_BYTES])
            _ensure_deep_probe_active(deadline, "compact 请求体")
        _prepare_deep_probe_io(connection, deadline, timeout, "compact 响应头")
        response = connection.getresponse()
        _ensure_deep_probe_active(deadline, "compact 响应头")
        status = int(response.status or 0)
        body = _read_bounded_probe_response(
            response,
            connection,
            deadline,
            timeout,
            max_bytes=64 * 1024,
            phase="compact 响应体",
        ).decode("utf-8", errors="replace")
        ok, detail = _classify_codex_compact_probe_response(status, body)
        return LocalAIProxyProbeResult(
            label="Codex 大请求网络近似",
            ok=ok,
            status=status,
            detail=detail,
            elapsed_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        return LocalAIProxyProbeResult(
            label="Codex 大请求网络近似",
            ok=False,
            detail=str(exc).splitlines()[0][:180] or type(exc).__name__,
            elapsed_ms=_elapsed_ms(started),
        )
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _download_exact_bytes_through_explicit_http_proxy(
    proxy_url: str,
    url: str,
    expected_bytes: int,
    *,
    timeout: int = 8,
) -> tuple[bool, int, str]:
    started = time.monotonic()
    deadline = started + LOCAL_PROXY_DEEP_OPERATION_DEADLINE_SECONDS
    connection = None
    received = 0
    try:
        initial_timeout = _prepare_deep_probe_io(
            None,
            deadline,
            timeout,
            "下载连接",
        )
        connection, target, target_path, host_header = _open_explicit_https_proxy_connection(
            proxy_url,
            url,
            initial_timeout,
        )
        _prepare_deep_probe_io(connection, deadline, timeout, "下载请求")
        connection.request(
            "GET",
            target_path,
            headers={
                "Host": host_header,
                "Accept": "application/octet-stream",
                "User-Agent": "API-Switcher/1.0",
            },
        )
        _prepare_deep_probe_io(connection, deadline, timeout, "下载响应头")
        response = connection.getresponse()
        _ensure_deep_probe_active(deadline, "下载响应头")
        status = int(response.status or 0)
        if status < 200 or status >= 300:
            return False, 0, f"HTTP {status or '无状态'}"
        while received <= expected_bytes:
            _prepare_deep_probe_io(connection, deadline, timeout, "下载响应体")
            chunk = response.read(
                min(LOCAL_PROXY_DEEP_IO_CHUNK_BYTES, expected_bytes + 1 - received)
            )
            _ensure_deep_probe_active(deadline, "下载响应体")
            if not chunk:
                break
            received += len(chunk)
        if received != expected_bytes:
            return False, received, f"期望 {expected_bytes} 字节，实收 {received} 字节"
        content_length = str(response.getheader("Content-Length") or "").strip()
        if content_length:
            try:
                if int(content_length) != expected_bytes:
                    return False, received, f"Content-Length={content_length} 与实收不一致"
            except ValueError:
                return False, received, f"Content-Length 非整数: {content_length}"
        return True, received, f"精确收到 {received} 字节"
    except Exception as exc:
        return False, received, str(exc).splitlines()[0][:200] or type(exc).__name__
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _upload_exact_bytes_through_explicit_http_proxy(
    proxy_url: str,
    url: str,
    expected_bytes: int,
    *,
    timeout: int = 8,
) -> tuple[bool, int, str]:
    started = time.monotonic()
    deadline = started + LOCAL_PROXY_DEEP_OPERATION_DEADLINE_SECONDS
    connection = None
    try:
        initial_timeout = _prepare_deep_probe_io(
            None,
            deadline,
            timeout,
            "上传连接",
        )
        connection, _target, target_path, host_header = _open_explicit_https_proxy_connection(
            proxy_url,
            url,
            initial_timeout,
        )
        separator = "&" if "?" in target_path else "?"
        upload_path = f"{target_path}{separator}bytes={expected_bytes}"
        _prepare_deep_probe_io(connection, deadline, timeout, "上传请求头")
        connection.putrequest(
            "POST",
            upload_path,
            skip_host=True,
            skip_accept_encoding=True,
        )
        connection.putheader("Host", host_header)
        connection.putheader("Accept", "application/json,text/plain,*/*")
        connection.putheader("Content-Type", "application/octet-stream")
        connection.putheader("Content-Length", str(expected_bytes))
        connection.putheader("User-Agent", "API-Switcher/1.0")
        connection.endheaders()
        chunk = b"0" * min(LOCAL_PROXY_DEEP_IO_CHUNK_BYTES, expected_bytes)
        sent = 0
        while sent < expected_bytes:
            _prepare_deep_probe_io(connection, deadline, timeout, "上传请求体")
            send_size = min(len(chunk), expected_bytes - sent)
            connection.send(chunk[:send_size])
            sent += send_size
            _ensure_deep_probe_active(deadline, "上传请求体")
        _prepare_deep_probe_io(connection, deadline, timeout, "上传响应头")
        response = connection.getresponse()
        _ensure_deep_probe_active(deadline, "上传响应头")
        status = int(response.status or 0)
        if status < 200 or status >= 300:
            return False, 0, f"HTTP {status or '无状态'}"
        acknowledged = _cloudflare_upload_acknowledged_bytes(response)
        if acknowledged != expected_bytes:
            return False, 0, f"服务端回传 {acknowledged} 字节，期望 {expected_bytes}"
        return True, expected_bytes, f"上传 {expected_bytes} 字节，服务端已核对"
    except Exception as exc:
        return False, 0, str(exc).splitlines()[0][:200] or type(exc).__name__
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _open_explicit_https_proxy_connection(
    proxy_url: str,
    url: str,
    timeout: float,
):
    proxy = urlparse(proxy_url)
    target = urlparse(url)
    if proxy.scheme.lower() != "http" or not proxy.hostname or not proxy.port:
        raise ValueError("临时代理地址必须是带端口的 HTTP 地址")
    if target.scheme.lower() != "https" or not target.hostname:
        raise ValueError("深度传输目标必须是 HTTPS 地址")
    target_port = int(target.port or 443)
    target_path = target.path or "/"
    if target.query:
        target_path += "?" + target.query
    connection = http.client.HTTPSConnection(
        proxy.hostname,
        int(proxy.port),
        timeout=max(0.05, min(60.0, float(timeout or 8))),
        context=ssl.create_default_context(),
    )
    connection.set_tunnel(target.hostname, target_port)
    host_header = target.hostname if target_port == 443 else f"{target.hostname}:{target_port}"
    return connection, target, target_path, host_header


def _ensure_deep_probe_active(deadline: float, phase: str) -> float:
    if _ISOLATED_MIHOMO_SHUTTING_DOWN.is_set():
        raise RuntimeError(f"应用正在退出，已取消{phase}")
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{phase}超过总截止时间")
    return remaining


def _prepare_deep_probe_io(
    connection,
    deadline: float,
    timeout: int | float,
    phase: str,
) -> float:
    """Check cancellation/deadline and tighten the next blocking socket operation."""

    remaining = _ensure_deep_probe_active(deadline, phase)
    try:
        base_timeout = float(timeout or 8)
    except (TypeError, ValueError):
        base_timeout = 8.0
    io_timeout = max(0.001, min(60.0, base_timeout, remaining))
    if connection is not None:
        connection.timeout = io_timeout
        sock = getattr(connection, "sock", None)
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            settimeout(io_timeout)
    return io_timeout


def _read_bounded_probe_response(
    response,
    connection,
    deadline: float,
    timeout: int | float,
    *,
    max_bytes: int,
    phase: str,
) -> bytes:
    content_length = str(response.getheader("Content-Length") or "").strip()
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ValueError(f"{phase} Content-Length 非整数") from exc
        if declared_length < 0 or declared_length > max_bytes:
            raise ValueError(f"{phase} Content-Length 超出限制")
    else:
        declared_length = None

    payload = bytearray()
    while len(payload) <= max_bytes:
        _prepare_deep_probe_io(connection, deadline, timeout, phase)
        chunk = response.read(
            min(16 * 1024, max_bytes + 1 - len(payload))
        )
        _ensure_deep_probe_active(deadline, phase)
        if not chunk:
            break
        payload.extend(chunk)
    if len(payload) > max_bytes:
        raise ValueError(f"{phase}超过 {max_bytes} 字节限制")
    if declared_length is not None and len(payload) != declared_length:
        raise ValueError(
            f"{phase}不完整：Content-Length={declared_length}，实收 {len(payload)}"
        )
    return bytes(payload)


def _cloudflare_upload_acknowledged_bytes(response, body: bytes = b"") -> int:
    """Return Cloudflare's exact byte receipt; all ambiguity fails closed."""

    del body  # Compatibility with older internal callers; body is never trusted as a receipt.
    values = []
    getheaders = getattr(response, "getheaders", None)
    if callable(getheaders):
        values = [
            str(value).strip()
            for key, value in getheaders()
            if str(key).strip().casefold() == "cf-meta-upload-bytes"
        ]
    else:
        getheader = getattr(response, "getheader", None)
        if callable(getheader):
            value = getheader("cf-meta-upload-bytes")
            if value is not None:
                values = [str(value).strip()]
    if not values:
        raise ValueError("上传响应缺少 cf-meta-upload-bytes 回执")
    if len(values) != 1:
        raise ValueError("上传响应包含重复的 cf-meta-upload-bytes 回执")
    if re.fullmatch(r"[0-9]+", values[0]) is None:
        raise ValueError("cf-meta-upload-bytes 回执不是合法非负整数")
    return int(values[0])


def _classify_codex_compact_probe_response(status: int, body: str) -> tuple[bool, str]:
    """Accept only a complete OpenAI-shaped unauthenticated response."""

    if status != 401:
        return False, f"HTTP {status or '无状态'}，未收到预期的无认证响应"
    try:
        parsed = json.loads(str(body or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, "HTTP 401，但结构化响应不完整"
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return False, "HTTP 401，但缺少 OpenAI error 结构"
    message = str(error.get("message") or "").casefold()
    error_type = str(error.get("type") or "").casefold()
    auth_challenge = any(
        token in f"{message} {error_type}"
        for token in ("api key", "authentication", "unauthorized", "invalid_api_key")
    )
    if not auth_challenge:
        return False, "HTTP 401，但未确认 OpenAI 无认证错误"
    return True, "HTTP 401 OpenAI 无认证结构完整（未调用真实 compact）"


def _classify_ai_probe_response(
    label: str,
    status: int,
    body: str,
    headers: dict[str, str] | None = None,
) -> tuple[bool, str, str]:
    """Validate that a response is recognizably from the intended AI service."""

    if status == 429:
        return False, "", "HTTP 429 限流"
    if status <= 0 or status >= 500:
        return False, "", f"HTTP {status or '无状态'}"
    text = str(body or "")[: 64 * 1024]
    lowered = text.lower()
    headers = headers or {}
    if label in {"ChatGPT 出口", "OpenAI/ChatGPT"}:
        trace = {}
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip():
                trace[key.strip().lower()] = value.strip()
        exit_country = trace.get("loc", "")
        identified = status == 200 and bool(exit_country) and bool(trace.get("ip") or trace.get("colo"))
        if not identified:
            return False, exit_country, f"HTTP {status}，响应不是有效 ChatGPT trace"
        if remote_proxy.proxy_region_is_hong_kong(exit_country):
            return False, exit_country, f"HTTP 200，ChatGPT 出口 loc={exit_country}（香港）"
        return True, exit_country, f"HTTP 200，ChatGPT 出口 loc={exit_country}"

    json_error_shape = '"error"' in lowered or '"type"' in lowered
    if label == "OpenAI API":
        try:
            parsed_error = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed_error = {}
        error = parsed_error.get("error") if isinstance(parsed_error, dict) else None
        identified = status == 401 and isinstance(error, dict) and bool(error)
        return identified, "", f"HTTP {status}" + (
            "，OpenAI API 身份已确认" if identified else "，未确认 OpenAI API 身份"
        )

    if label == "Claude/Anthropic":
        identified = (
            status in {400, 401}
            and (
                json_error_shape
                and any(
                    token in lowered
                    for token in (
                        "api key",
                        "authentication",
                        "unauthorized",
                        "x-api-key",
                        "anthropic-version",
                    )
                )
            )
        )
        return identified, "", f"HTTP {status}" + ("，Anthropic 身份已确认" if identified else "，未确认 Anthropic 身份")

    if label == "Gemini/Google AI":
        identified = False
        if status in {400, 401, 403} and json_error_shape:
            try:
                parsed_error = json.loads(text)
            except (json.JSONDecodeError, TypeError, ValueError):
                parsed_error = {}
            error = parsed_error.get("error") if isinstance(parsed_error, dict) else None
            error = error if isinstance(error, dict) else {}
            message = str(error.get("message") or "").casefold()
            error_status = str(error.get("status") or "").casefold()
            credential_challenge = any(
                token in message
                for token in ("api key", "unregistered caller", "credential")
            )
            policy_rejection = any(
                token in message
                for token in ("region", "country", "disabled", "quota", "policy", "blocked")
            )
            identified = (
                credential_challenge
                and not policy_rejection
                and error_status in {"", "permission_denied", "unauthenticated", "invalid_argument"}
            )
        return identified, "", f"HTTP {status}" + ("，Google AI 身份已确认" if identified else "，未确认 Google AI 身份")
    return False, "", "未知 AI 探测目标"


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _write_mihomo_payload(target: Path, url: str, payload: bytes) -> None:
    lower_url = url.lower()
    if lower_url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            exe_names = [
                name
                for name in archive.namelist()
                if name.lower().endswith(".exe") and ("mihomo" in name.lower() or "clash" in name.lower())
            ]
            if not exe_names:
                raise RuntimeError("mihomo zip 里没有找到可执行文件")
            content = archive.read(exe_names[0])
    elif lower_url.endswith(".gz"):
        content = gzip.decompress(payload)
    else:
        content = payload
    atomic_write_bytes(target, content)


def _read_pid() -> int | None:
    try:
        return int(LOCAL_PROXY_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _managed_local_proxy_is_running(state: dict | None = None) -> bool:
    pid = _read_pid()
    return bool(pid and _is_pid_running(pid) and _is_managed_mihomo_pid(pid, state=state or _load_state()))


def _is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        return _is_windows_pid_running(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_windows_pid_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _windows_process_image_path(pid: int) -> str:
    if os.name != "nt":
        return ""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _normalize_existing_path(path: str | Path | None) -> str:
    if not path:
        return ""
    try:
        return os.path.normcase(str(Path(path).resolve(strict=False)))
    except Exception:
        return os.path.normcase(str(path))


def _is_managed_mihomo_pid(
    pid: int,
    state: dict | None = None,
    binary_path: str | Path | None = None,
) -> bool:
    if not pid or not _is_pid_running(pid):
        return False
    if os.name != "nt":
        return True

    image_path = _windows_process_image_path(pid)
    if not image_path:
        return False
    image_name = Path(image_path).name.lower()
    if image_name not in {"mihomo.exe", "clash.exe"}:
        return False

    expected_paths = []
    if binary_path:
        expected_paths.append(binary_path)
    stored_binary = (state or {}).get("binary_path") if isinstance(state, dict) else None
    if stored_binary:
        expected_paths.append(stored_binary)
    expected_paths.append(LOCAL_PROXY_BIN_DIR / "mihomo.exe")

    image_normalized = _normalize_existing_path(image_path)
    for expected in expected_paths:
        if image_normalized and image_normalized == _normalize_existing_path(expected):
            return True
    return False


def _cleanup_managed_process(binary_path: Path, state: dict | None = None) -> None:
    pid = _read_pid()
    if pid and _is_managed_mihomo_pid(pid, state=state, binary_path=binary_path):
        _terminate_pid(pid)
    LOCAL_PROXY_PID_PATH.unlink(missing_ok=True)


def _terminate_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    for _ in range(20):
        if not _is_pid_running(pid):
            return True
        time.sleep(0.2)
    _force_terminate_pid(pid)
    for _ in range(10):
        if not _is_pid_running(pid):
            return True
        time.sleep(0.2)
    return False


def _force_terminate_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=10,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        return


def _is_port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def _start_local_mihomo(binary_path: Path, mixed_port: int) -> None:
    pid = _read_pid()
    if pid and _is_pid_running(pid):
        state = _load_state()
        if _is_managed_mihomo_pid(pid, state=state, binary_path=binary_path):
            if not _terminate_pid(pid):
                raise RuntimeError(f"无法停止已有本机 AI 代理进程 PID {pid}")
            if _is_port_listening(mixed_port):
                raise RuntimeError(f"本机端口 127.0.0.1:{mixed_port} 仍被占用，请稍后重试")
        else:
            LOCAL_PROXY_PID_PATH.unlink(missing_ok=True)
            if _is_port_listening(mixed_port):
                raise RuntimeError(f"本机端口 127.0.0.1:{mixed_port} 已被其他程序占用")
    elif _is_port_listening(mixed_port):
        raise RuntimeError(f"本机端口 127.0.0.1:{mixed_port} 已被占用，请先关闭占用该端口的程序")

    LOCAL_PROXY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    with LOCAL_PROXY_LOG_PATH.open("ab") as log_handle:
        log_handle.write(f"\n--- API切换器 start {remote_proxy._now_iso()} port={mixed_port} ---\n".encode("utf-8"))
        process = subprocess.Popen(
            [str(binary_path), "-d", str(LOCAL_PROXY_CONFIG_DIR)],
            cwd=str(LOCAL_PROXY_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    LOCAL_PROXY_PID_PATH.write_text(str(process.pid), encoding="utf-8")
    time.sleep(1.5)
    if process.poll() is not None:
        raise RuntimeError(_mihomo_failure_message("mihomo 启动失败"))
    for _ in range(10):
        if _is_port_listening(mixed_port):
            return
        time.sleep(0.5)
    raise RuntimeError(_mihomo_failure_message(f"mihomo 已启动但端口 {mixed_port} 未监听"))


def _mihomo_failure_message(prefix: str) -> str:
    tail = _read_log_tail()
    if not tail:
        return f"{prefix}，详见日志: {LOCAL_PROXY_LOG_PATH}"
    return f"{prefix}，详见日志: {LOCAL_PROXY_LOG_PATH}；最近日志: {tail}"


def _read_log_tail(max_lines: int = 8, max_chars: int = 1000) -> str:
    try:
        text = LOCAL_PROXY_LOG_PATH.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    tail = "\n".join(lines[-max(1, max_lines):])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail
