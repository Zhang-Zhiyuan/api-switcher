from __future__ import annotations

import gzip
import ipaddress
import io
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from config.paths import STORAGE_DIR
from core import persistent_env, remote_proxy, vscode_parser


DEFAULT_LOCAL_MIXED_PORT = 17897
LOCAL_PORT_CANDIDATES = tuple(range(DEFAULT_LOCAL_MIXED_PORT, DEFAULT_LOCAL_MIXED_PORT + 50))
LOCAL_PROXY_DIR = STORAGE_DIR / "local_ai_proxy"
LOCAL_PROXY_CONFIG_DIR = LOCAL_PROXY_DIR / "mihomo"
LOCAL_PROXY_BIN_DIR = LOCAL_PROXY_DIR / "bin"
LOCAL_PROXY_STATE_PATH = LOCAL_PROXY_DIR / "state.json"
LOCAL_PROXY_PREFS_PATH = LOCAL_PROXY_DIR / "preferences.json"
LOCAL_PROXY_LOG_PATH = LOCAL_PROXY_DIR / "mihomo.log"
LOCAL_PROXY_PID_PATH = LOCAL_PROXY_DIR / "mihomo.pid"
MIHOMO_DOWNLOAD_RETRIES = 3
WINDOWS_SYSTEM_PROXY_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
WINDOWS_SYSTEM_PROXY_KEYS = ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL", "AutoDetect")
WINDOWS_SYSTEM_PROXY_OVERRIDE = "<local>;127.0.0.1;localhost;::1"
LOCAL_AI_PROBE_TARGETS = (
    ("OpenAI/ChatGPT", "https://chatgpt.com/cdn-cgi/trace"),
    ("Claude/Anthropic", "https://api.anthropic.com/"),
    ("Gemini/Google AI", "https://generativelanguage.googleapis.com/"),
)
LOCAL_PROXY_BUILTIN_SITES = (
    {
        "id": "youtube",
        "label": "YouTube",
        "targets": ("youtube.com", "youtu.be", "ytimg.com", "googlevideo.com"),
    },
    {
        "id": "google",
        "label": "Google 搜索/账号",
        "targets": ("google.com", "gstatic.com", "googleapis.com", "googleusercontent.com"),
    },
    {
        "id": "github",
        "label": "GitHub",
        "targets": ("github.com", "githubusercontent.com", "githubassets.com", "github.io"),
    },
    {
        "id": "huggingface",
        "label": "Hugging Face",
        "targets": ("huggingface.co", "hf.co"),
    },
    {
        "id": "x_twitter",
        "label": "X / Twitter",
        "targets": ("x.com", "twitter.com", "twimg.com", "t.co"),
    },
    {
        "id": "reddit",
        "label": "Reddit",
        "targets": ("reddit.com", "redd.it", "redditstatic.com", "redditmedia.com"),
    },
    {
        "id": "discord",
        "label": "Discord",
        "targets": ("discord.com", "discordapp.com", "discord.gg", "discordcdn.com"),
    },
    {
        "id": "telegram",
        "label": "Telegram",
        "targets": ("telegram.org", "t.me", "tdesktop.com"),
    },
)
LOCAL_PROXY_BUILTIN_SITE_IDS = {str(item["id"]) for item in LOCAL_PROXY_BUILTIN_SITES}
LOCAL_PROXY_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9][a-z0-9-]{0,62}(\.[a-z0-9][a-z0-9-]{0,62})+$",
    re.IGNORECASE,
)
_LOCAL_PROXY_PREFS_LOCK = threading.RLock()


@dataclass(frozen=True)
class LocalAIProxyStatus:
    installed: bool
    running: bool
    config_path: str
    proxy_url: str
    detail: str = ""

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

    def summary(self) -> str:
        prefix = "可达" if self.ok else "失败"
        status = f"HTTP {self.status}" if self.status else self.detail
        elapsed = f"{self.elapsed_ms}ms" if self.elapsed_ms else ""
        pieces = [piece for piece in (prefix, status, elapsed) if piece]
        return f"{self.label}: {' / '.join(pieces)}"


def load_local_proxy_preferences() -> dict:
    with _LOCAL_PROXY_PREFS_LOCK:
        try:
            data = json.loads(LOCAL_PROXY_PREFS_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        return _normalize_local_proxy_preferences(data)


def save_local_proxy_preferences(**updates) -> dict:
    with _LOCAL_PROXY_PREFS_LOCK:
        preferences = load_local_proxy_preferences()
        preferences.update({key: value for key, value in updates.items() if value is not None})
        preferences = _normalize_local_proxy_preferences(preferences)
        preferences["updated_at"] = remote_proxy._now_iso()
        _ensure_local_dirs()
        temp_path = LOCAL_PROXY_PREFS_PATH.with_name(f"{LOCAL_PROXY_PREFS_PATH.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(json.dumps(preferences, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(LOCAL_PROXY_PREFS_PATH)
        finally:
            temp_path.unlink(missing_ok=True)
        return preferences


def local_proxy_start_on_login_enabled() -> bool:
    return bool(load_local_proxy_preferences().get("start_on_login"))


def set_local_proxy_start_on_login(enabled: bool) -> dict:
    return save_local_proxy_preferences(start_on_login=bool(enabled))


def set_local_proxy_startup_node(proxy_text: str) -> str:
    proxy_node = remote_proxy.parse_proxy_node(proxy_text)
    _save_last_proxy_node_strict(proxy_node)
    return remote_proxy.describe_proxy_node(proxy_node)


def local_proxy_startup_node_summary() -> str:
    node = _load_last_proxy_node()
    return remote_proxy.describe_proxy_node(node) if node else ""


def set_local_proxy_non_cn_mode(enabled: bool) -> dict:
    return save_local_proxy_preferences(proxy_non_cn=bool(enabled))


def set_builtin_proxy_site_enabled(site_id: str, enabled: bool) -> dict:
    site_key = str(site_id or "").strip()
    if site_key not in LOCAL_PROXY_BUILTIN_SITE_IDS:
        raise ValueError(f"未知内置站点: {site_id}")
    preferences = load_local_proxy_preferences()
    builtin_sites = dict(preferences.get("builtin_sites") or {})
    builtin_sites[site_key] = bool(enabled)
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
            entry["enabled"] = bool(enabled)
            entry["target"] = normalized["target"]
            save_local_proxy_preferences(custom_targets=entries)
            return entry
    entry = {
        "id": uuid.uuid4().hex,
        "target": normalized["target"],
        "kind": normalized["kind"],
        "value": normalized["value"],
        "enabled": bool(enabled),
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
            entry["enabled"] = bool(enabled)
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
    if not local_proxy_start_on_login_enabled():
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
    node = _load_last_proxy_node() or _read_local_managed_proxy_node()
    if not node:
        return "Win11 本机代理自启已开启，但还没有保存过可启动节点"
    return install_local_ai_proxy(remote_proxy.format_proxy_node(node))


def install_local_ai_proxy(proxy_text: str, mixed_port: int = DEFAULT_LOCAL_MIXED_PORT) -> str:
    if os.name != "nt":
        raise RuntimeError("本机 AI 代理目前只支持 Windows")
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

    config_path.write_text(_build_local_mihomo_config(proxy_node, mixed_port), encoding="utf-8")
    try:
        _start_local_mihomo(binary_path, mixed_port)
        _apply_local_env(mixed_port)
        _apply_local_vscode_proxy(mixed_port)
        _apply_windows_system_proxy(mixed_port)
    except Exception as exc:
        restore_errors = _restore_managed_settings(state, mixed_port)
        _cleanup_managed_process(binary_path, state)
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
            "pid": _read_pid(),
            "node_display": remote_proxy.describe_proxy_node(proxy_node),
            "node_key": remote_proxy.proxy_node_key(proxy_node),
            "node_name": str(proxy_node.get("name") or ""),
            "controller_port": remote_proxy.mihomo_controller_port(mixed_port),
            "updated_at": remote_proxy._now_iso(),
        }
    )
    _save_state(state)
    _save_last_proxy_node(proxy_node)
    return (
        f"本机 AI 代理已启动: {proxy_url}；"
        "已写入 Windows 用户环境变量、VS Code 本机设置和当前用户系统代理，"
        "并临时关闭系统 PAC/自动检测代理；新终端或重开的 VS Code 窗口生效"
    )


def reload_local_ai_proxy(proxy_text: str, mixed_port: int = DEFAULT_LOCAL_MIXED_PORT) -> str:
    if os.name != "nt":
        raise RuntimeError("本机 AI 代理目前只支持 Windows")
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
    new_config = _build_local_mihomo_config(proxy_node, mixed_port)
    if old_config.strip() == new_config.strip():
        return "本机 AI 代理运行节点已是最新配置，无需热更新"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(new_config, encoding="utf-8")
    try:
        _reload_local_mihomo_config(config_path, mixed_port)
    except Exception as exc:
        if old_config:
            config_path.write_text(old_config, encoding="utf-8")
        raise RuntimeError(f"当前本机代理不支持无感热更新或控制口不可用: {exc}") from exc

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
    _save_state(state)
    _save_last_proxy_node(proxy_node)
    _remember_selected_subscription_node(proxy_node)
    return f"本机 AI 代理已热更新节点为 {remote_proxy.describe_proxy_node(proxy_node)}"


def reload_local_ai_proxy_verified(
    proxy_text: str,
    candidate_nodes=None,
    max_candidates: int = 10,
) -> str:
    requested_node = remote_proxy.parse_proxy_node(proxy_text)
    requested_key = remote_proxy.proxy_node_key(requested_node)
    original_node = _read_local_managed_proxy_node()
    try:
        reload_message = reload_local_ai_proxy(proxy_text)
    except Exception as exc:
        return f"本机 AI 代理自动更新跳过，{exc}"
    if "跳过" in reload_message or "无需热更新" in reload_message:
        return reload_message

    probe_message = probe_local_ai_proxy()
    if remote_proxy._probe_summary_all_ok(probe_message):
        return f"{reload_message}；验证通过: {remote_proxy._compact_probe_summary(probe_message)}"

    candidates = tuple(item for item in (candidate_nodes or []) if isinstance(item, remote_proxy.ProxySubscriptionNode))
    if not candidates:
        restore_suffix = _restore_local_proxy_node_after_failed_update(original_node, requested_node)
        return f"{reload_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}{restore_suffix}"

    try:
        latencies = remote_proxy.measure_proxy_node_latencies(
            candidates,
            timeout=3.0,
            attempts=2,
            max_workers=20,
        )
    except Exception as exc:
        restore_suffix = _restore_local_proxy_node_after_failed_update(original_node, requested_node)
        return (
            f"{reload_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}；"
            f"自动换节点测速失败: {exc}{restore_suffix}"
        )

    ranked = []
    for item in remote_proxy.sort_proxy_subscription_nodes(candidates, latencies):
        key = remote_proxy.proxy_node_key(item.node)
        if key == requested_key:
            continue
        result = latencies.get(key)
        latency = remote_proxy.proxy_node_latency_ms(result)
        if latency is None or not remote_proxy.proxy_node_latency_ok(result):
            continue
        ranked.append((latency, item, result))

    attempts = max(1, min(remote_proxy._int_or_default(max_candidates, 10), len(ranked)))
    for _latency, item, result in ranked[:attempts]:
        try:
            reload_local_ai_proxy(remote_proxy.format_proxy_node(item.node))
            candidate_probe = probe_local_ai_proxy()
        except Exception:
            continue
        if remote_proxy._probe_summary_all_ok(candidate_probe):
            _remember_selected_subscription_node(item.node)
            return (
                f"本机 AI 代理原热更新节点验证失败，已无重启切换到 {remote_proxy.describe_proxy_node(item.node)}"
                f"（本机 TCP {remote_proxy.proxy_node_latency_label(result)}）；"
                f"验证通过: {remote_proxy._compact_probe_summary(candidate_probe)}"
            )

    restore_suffix = _restore_local_proxy_node_after_failed_update(original_node, requested_node)
    return (
        f"{reload_message}；验证未完全通过: {remote_proxy._compact_probe_summary(probe_message)}；"
        f"自动尝试 {attempts} 个节点仍未 3/3 可达{restore_suffix}"
    )


def refresh_running_local_ai_proxy_from_subscription(nodes) -> str:
    state = _load_state()
    mixed_port = remote_proxy._normalize_port(
        state.get("mixed_port") or DEFAULT_LOCAL_MIXED_PORT,
        "本机代理端口",
    )
    if not _managed_local_proxy_is_running(state) or not _is_port_listening(mixed_port):
        return "本机 AI 代理未运行，已跳过订阅自动热更新"
    candidates = tuple(item for item in (nodes or []) if isinstance(item, remote_proxy.ProxySubscriptionNode))
    if not candidates:
        return "订阅里没有可用节点，已跳过本机热更新"
    current_node = _read_local_managed_proxy_node()
    chosen = remote_proxy._find_matching_subscription_node(candidates, current_node) if current_node else None
    if chosen is None:
        try:
            latencies = remote_proxy.measure_proxy_node_latencies(
                candidates,
                timeout=3.0,
                attempts=2,
                max_workers=20,
            )
        except Exception as exc:
            return f"订阅已刷新，但本机节点测速失败，已保留当前运行节点: {exc}"
        ranked = [
            item
            for item in remote_proxy.sort_proxy_subscription_nodes(candidates, latencies)
            if remote_proxy.proxy_node_latency_ok(latencies.get(remote_proxy.proxy_node_key(item.node)))
        ]
        if not ranked:
            return "订阅已刷新，但没有测到可连节点，已保留当前运行节点"
        chosen = ranked[0]
    return reload_local_ai_proxy_verified(remote_proxy.format_proxy_node(chosen.node), candidates)


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
    details = []
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
        running=managed_pid_running or (installed and port_listening),
        config_path=str(config_path),
        proxy_url=_proxy_url(mixed_port),
        detail="；".join(details),
    )


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


def _ensure_local_dirs() -> None:
    LOCAL_PROXY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_PROXY_BIN_DIR.mkdir(parents=True, exist_ok=True)


def _normalize_local_proxy_preferences(data: dict | None) -> dict:
    raw = data if isinstance(data, dict) else {}
    builtin_sites = {}
    raw_sites = raw.get("builtin_sites")
    if isinstance(raw_sites, dict):
        for site_id, enabled in raw_sites.items():
            site_key = str(site_id or "").strip()
            if site_key in LOCAL_PROXY_BUILTIN_SITE_IDS:
                builtin_sites[site_key] = bool(enabled)

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
                "enabled": bool(item.get("enabled", True)),
                "created_at": str(item.get("created_at") or remote_proxy._now_iso()),
            })

    last_node = raw.get("last_node") if isinstance(raw.get("last_node"), dict) else None
    if last_node:
        try:
            last_node = remote_proxy._normalize_proxy_node(last_node)
        except Exception:
            last_node = None

    return {
        "start_on_login": bool(raw.get("start_on_login")),
        "proxy_non_cn": bool(raw.get("proxy_non_cn")),
        "builtin_sites": builtin_sites,
        "custom_targets": custom_targets,
        "last_node": last_node,
        "updated_at": str(raw.get("updated_at") or ""),
    }


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
    preferences = preferences or load_local_proxy_preferences()
    domains = []
    ip_cidrs = []
    builtin_sites = preferences.get("builtin_sites") if isinstance(preferences.get("builtin_sites"), dict) else {}
    for site in LOCAL_PROXY_BUILTIN_SITES:
        if builtin_sites.get(str(site["id"])):
            domains.extend(str(target) for target in site.get("targets") or ())
    for entry in preferences.get("custom_targets") or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        if entry.get("kind") == "ip-cidr":
            ip_cidrs.append(str(entry.get("value") or ""))
        elif entry.get("kind") == "domain":
            domains.append(str(entry.get("value") or ""))
    return {
        "extra_proxy_domains": tuple(domains),
        "extra_proxy_ip_cidrs": tuple(ip_cidrs),
        "proxy_non_cn": bool(preferences.get("proxy_non_cn")),
    }


def _build_local_mihomo_config(proxy_node: dict, mixed_port: int) -> str:
    return remote_proxy.build_mihomo_config(
        proxy_node,
        mixed_port,
        **_routing_options_from_preferences(),
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


def _remember_selected_subscription_node(proxy_node: dict) -> None:
    try:
        remote_proxy.set_proxy_subscription_selected_node(proxy_node)
    except Exception:
        return


def _load_state() -> dict:
    try:
        data = json.loads(LOCAL_PROXY_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    _ensure_local_dirs()
    temp_path = LOCAL_PROXY_STATE_PATH.with_name(f"{LOCAL_PROXY_STATE_PATH.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(LOCAL_PROXY_STATE_PATH)
    finally:
        temp_path.unlink(missing_ok=True)


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


def _restore_local_proxy_node_after_failed_update(original_node: dict | None, attempted_node: dict | None) -> str:
    if not original_node:
        return "；未读取到更新前节点，已保留最后一次热更新状态"
    try:
        original = remote_proxy._normalize_proxy_node(original_node)
    except Exception:
        return "；更新前节点格式不可恢复，已保留最后一次热更新状态"
    try:
        reload_local_ai_proxy(remote_proxy.format_proxy_node(original))
    except Exception as exc:
        attempted = remote_proxy.describe_proxy_node(attempted_node or {}) if attempted_node else "当前节点"
        return f"；尝试从 {attempted} 恢复更新前节点失败: {exc}"
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
        return remote_proxy._normalize_proxy_node(node)
    except Exception:
        return None


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
    started = time.monotonic()
    try:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "*/*",
                "User-Agent": "API-Switcher/1.0",
            },
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({
                "http": proxy_url,
                "https": proxy_url,
            })
        )
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
            return LocalAIProxyProbeResult(
                label=label,
                ok=0 < status < 500,
                status=status,
                elapsed_ms=_elapsed_ms(started),
            )
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        return LocalAIProxyProbeResult(
            label=label,
            ok=0 < status < 500,
            status=status,
            elapsed_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        return LocalAIProxyProbeResult(
            label=label,
            ok=False,
            detail=str(exc).splitlines()[0][:120],
            elapsed_ms=_elapsed_ms(started),
        )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _write_mihomo_payload(target: Path, url: str, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
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
                temp_path.write_bytes(archive.read(exe_names[0]))
        elif lower_url.endswith(".gz"):
            temp_path.write_bytes(gzip.decompress(payload))
        else:
            temp_path.write_bytes(payload)
        temp_path.replace(target)
    finally:
        temp_path.unlink(missing_ok=True)


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
