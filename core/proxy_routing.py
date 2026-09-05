"""Shared service-route editing and per-SSH-host routing authority.

Only profile IDs and node fingerprints are stored here. Subscription credentials
stay in the existing subscription cache; each deployment resolves a fresh snapshot.
"""
from __future__ import annotations

import copy
from functools import wraps
import hashlib
import json
import threading

from core.atomic_io import atomic_write_text
from core.lazy_imports import LazyModule
from core.local_proxy_constants import (
    LOCAL_PROXY_AI_SERVICES,
    LOCAL_PROXY_BUILTIN_SITES,
    LOCAL_PROXY_SERVICE_ROUTE_IDS,
)

local_proxy = LazyModule("core.local_proxy")
remote_proxy = LazyModule("core.remote_proxy")

ROUTE_KEYS = (
    "builtin_sites", "custom_targets", "service_profile_bindings", "service_node_bindings",
)
_HOST_LOCKS: dict[str, threading.RLock] = {}
_HOST_LOCKS_GUARD = threading.Lock()
_BINDINGS_LOCK = threading.RLock()


def serialized_binding_change(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _BINDINGS_LOCK:
            return function(*args, **kwargs)
    return wrapped


def service_ids(preferences: dict) -> set[str]:
    return LOCAL_PROXY_SERVICE_ROUTE_IDS | {
        f"custom:{item['id']}"
        for item in preferences.get("custom_targets") or ()
        if isinstance(item, dict) and item.get("id")
    }


def node_bindings(preferences: dict, *, strict: bool = True) -> dict[str, str]:
    raw = preferences.get("service_node_bindings", {})
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("service_node_bindings 节点绑定必须是对象")
        return {}
    allowed = service_ids(preferences)
    result = {}
    for service, key in raw.items():
        if service not in allowed:
            continue
        if not isinstance(key, str) or len(key) > 128 or any(ord(c) < 32 for c in key):
            if strict:
                raise ValueError(f"{service} 的固定节点标识无效")
            continue
        if key.strip():
            if not (preferences.get("service_profile_bindings") or {}).get(service):
                if strict:
                    raise ValueError(f"{service} 的固定节点没有对应订阅，请重新选择线路")
                continue
            result[service] = key.strip()
    return result


def route_snapshot(preferences: dict) -> dict:
    return {
        key: copy.deepcopy(preferences.get(key, [] if key == "custom_targets" else {}))
        for key in ROUTE_KEYS
    }


def normalize_routes(preferences: dict) -> dict:
    if not isinstance(preferences, dict):
        raise ValueError("服务分流配置必须是对象")
    for key in ROUTE_KEYS:
        if key in preferences and not isinstance(
            preferences[key], list if key == "custom_targets" else dict
        ):
            labels = {"builtin_sites": "站点开关", "custom_targets": "自定义目标",
                      "service_profile_bindings": "订阅绑定", "service_node_bindings": "节点绑定"}
            raise ValueError(f"服务分流的{labels[key]}格式无效")
    normalized = local_proxy._normalize_local_proxy_preferences(preferences)
    normalized["service_profile_bindings"] = (
        local_proxy._service_profile_bindings_authority_value(preferences)
    )
    if set(normalized["service_profile_bindings"]) - service_ids(normalized):
        raise ValueError("订阅绑定指向无效的自定义目标，请先修正域名或 IP")
    normalized["service_node_bindings"] = node_bindings(preferences)
    return route_snapshot(normalized)


def config_options(preferences: dict) -> dict:
    options = local_proxy._routing_options_from_preferences(preferences)
    # Scope/strict-privacy are owned by each existing deployment's settings.
    options.pop("strict_privacy", None)
    options.pop("proxy_non_cn", None)
    return {**options, **local_proxy._resolve_service_subscription_routes(preferences)}


def validate_routes(preferences: dict) -> dict:
    normalized = normalize_routes(preferences)
    # Also validate disabled bindings: selecting an unavailable source must be
    # actionable in the editor, never silently saved as a future broken route.
    for service, profile_id in normalized["service_profile_bindings"].items():
        profile = local_proxy._proxy_subscription_profile_for_route(profile_id)
        local_proxy._selected_subscription_route_pool(
            profile, ai_sensitive=False,
            node_key=normalized["service_node_bindings"].get(service, ""),
        )
    remote_proxy.build_mihomo_config(
        {"name": "validation", "type": "http", "server": "127.0.0.1", "port": 9},
        **config_options(normalized),
    )
    return normalized


def route_rows(preferences: dict) -> list[dict]:
    rows = []
    for item in (*LOCAL_PROXY_AI_SERVICES, *LOCAL_PROXY_BUILTIN_SITES):
        always = item in LOCAL_PROXY_AI_SERVICES
        rows.append({
            "id": item["id"], "label": item["label"], "always": always,
            "enabled": always or bool((preferences.get("builtin_sites") or {}).get(item["id"])),
        })
    # Keep the legacy shared custom route editable for existing installations.
    rows.append({"id": "custom", "label": "自定义目标默认线路", "always": True, "enabled": True})
    for item in preferences.get("custom_targets") or ():
        rows.append({
            "id": f"custom:{item['id']}", "label": item.get("target") or item["value"],
            "always": False, "enabled": bool(item.get("enabled", True)),
        })
    return rows


def load_route_catalog() -> list[dict]:
    """Load/cache once on a worker; return only non-secret display metadata."""
    state = remote_proxy.load_proxy_subscription_state()
    result = []
    for profile in remote_proxy.list_proxy_subscription_profiles(state):
        entry = {"id": profile["id"], "name": profile.get("name") or "未命名订阅", "nodes": []}
        try:
            cached = remote_proxy.load_cached_proxy_subscription(profile)
            if cached:
                entry["nodes"] = [
                    {"key": remote_proxy.proxy_subscription_node_key(item),
                     "label": str(item.node.get("name") or f"节点 {index}")}
                    for index, item in enumerate(cached.nodes, 1)
                    if not str(item.node.get("dialer-proxy") or "").strip()
                ]
        except Exception:
            entry["error"] = "缓存读取失败，请重新拉取"
        entry["selected_node_key"] = profile.get("selected_node_key") or ""
        result.append(entry)
    return result


def _host_path(ssh_name: str):
    if not isinstance(ssh_name, str) or not ssh_name.strip():
        raise ValueError("SSH 目标不能为空")
    digest = hashlib.sha256(ssh_name.encode("utf-8")).hexdigest()
    return remote_proxy.STORAGE_DIR / "ssh_proxy_routes" / f"{digest}.json"


def host_lock(ssh_name: str):
    with _HOST_LOCKS_GUARD:
        return _HOST_LOCKS.setdefault(str(_host_path(ssh_name)), threading.RLock())


def serialized_ssh_route_operation(function):
    @wraps(function)
    def wrapped(ssh_name, *args, **kwargs):
        with host_lock(ssh_name):
            return function(ssh_name, *args, **kwargs)
    return wrapped


def load_ssh_routes(ssh_name: str) -> dict:
    with host_lock(ssh_name):
        try:
            raw = json.loads(_host_path(ssh_name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return route_snapshot({})
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{ssh_name}: 服务分流配置读取失败，已停止覆盖线路") from exc
        if not isinstance(raw, dict) or raw.get("ssh_name") != ssh_name:
            raise ValueError(f"{ssh_name}: 服务分流配置的服务器标识不匹配")
        return normalize_routes(raw.get("routes"))


def _save_ssh_routes(ssh_name: str, preferences: dict):
    atomic_write_text(_host_path(ssh_name), json.dumps(
        {"ssh_name": ssh_name, "routes": route_snapshot(preferences)},
        ensure_ascii=False, indent=2,
    ))


def ssh_config_options(ssh_name: str, old_config: str = "", override=None) -> dict:
    if override is None and not _host_path(ssh_name).exists() and "API-SWITCHER-SUB-" in old_config:
        raise RuntimeError(f"{ssh_name}: 远端已有独立线路，但本机没有对应绑定，请先打开“目标分流”重新设置")
    return config_options(load_ssh_routes(ssh_name) if override is None else override)


def ssh_probe_kwargs(ssh_name: str) -> dict:
    routes = load_ssh_routes(ssh_name)
    return {"routing_preferences": routes} if routes["service_profile_bindings"] else {}


def ssh_bindings_for_profile(profile_id: str) -> tuple[str, ...]:
    directory = remote_proxy.STORAGE_DIR / "ssh_proxy_routes"
    bindings = []
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name = data["ssh_name"]
            if path != _host_path(name):
                raise ValueError("SSH 分流文件名与目标不匹配")
            routes = load_ssh_routes(name)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("SSH 服务分流配置无法读取，无法确认订阅引用") from exc
        for service, bound in routes["service_profile_bindings"].items():
            if bound == profile_id:
                bindings.append(f"{name} / {local_proxy._local_proxy_service_label(service)}")
    return tuple(bindings)


@serialized_binding_change
@serialized_ssh_route_operation
def apply_ssh_routes(ssh_name: str, preferences: dict, *, expected=None, mixed_port=7890) -> str:
    previous = load_ssh_routes(ssh_name)
    if expected is not None and previous != route_snapshot(expected):
        raise RuntimeError(f"{ssh_name}: 线路已被其他操作修改，请重新打开编辑器")
    updated = validate_routes(preferences)
    status = remote_proxy.inspect_ai_proxy(ssh_name, mixed_port)
    current = None
    message = "代理未运行；已保存，下次部署生效"
    if status.running:
        current = remote_proxy._read_remote_managed_proxy_node(ssh_name, mixed_port)
        if current is None:
            raise RuntimeError(f"{ssh_name}: 无法读取默认线路，已取消分流变更")
    # Persist before reloading, while holding the host lock. A failed save must
    # never alter the live route. A failed reload restores its exact raw config;
    # rebuilding the old preference from a refreshed cache could lose old nodes.
    try:
        _save_ssh_routes(ssh_name, updated)
    except Exception as exc:
        raise RuntimeError(f"{ssh_name}: 保存线路失败，已保留原绑定") from exc
    if current:
        try:
            message = remote_proxy.reload_ai_proxy(
                ssh_name, remote_proxy.format_proxy_node(current), mixed_port,
                persist_selection=False, routing_preferences=updated,
            )
            if "已跳过" in message:
                raise RuntimeError("代理运行状态已变化，请重新检查后应用")
        except Exception as exc:
            try:
                _save_ssh_routes(ssh_name, previous)
            except Exception as rollback:
                raise RuntimeError(f"{ssh_name}: 应用线路失败，原绑定回滚也失败: {rollback}") from exc
            raise RuntimeError(f"{ssh_name}: 应用线路失败，已恢复原绑定: {exc}") from exc
    return f"{ssh_name}: 目标分流已保存；{message}"
