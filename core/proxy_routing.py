"""Shared service-route editing and per-SSH-host routing authority.

Only profile IDs and node fingerprints are stored here. Subscription credentials
stay in the existing subscription cache; each deployment resolves a fresh snapshot.
"""
from __future__ import annotations

import base64
import binascii
import copy
from contextlib import ExitStack, contextmanager
from functools import wraps
import hashlib
import json
import threading

from core.atomic_io import atomic_write_bytes, atomic_write_text
from core.lazy_imports import LazyModule
from core.local_proxy_constants import (
    LOCAL_PROXY_AI_SERVICES,
    LOCAL_PROXY_BUILTIN_SITES,
    LOCAL_PROXY_SERVICE_ROUTE_IDS,
)

local_proxy = LazyModule("core.local_proxy")
remote_proxy = LazyModule("core.remote_proxy")

ROUTE_KEYS = (
    "builtin_sites", "custom_targets", "service_profile_bindings", "service_node_bindings", "service_route_modes",
    "service_node_pools",
)
MAX_SERVICE_NODE_POOL_SIZE = 16
ROUTE_SNAPSHOT_MARKER = "# API-Switcher-Routes-v1: "
MAX_ROUTE_SNAPSHOT_BYTES = 262144
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


def route_modes(preferences: dict) -> dict[str, str]:
    """Preserve explicit default/direct choices across editor sessions.

    Absence means the service is eligible for a draft suggestion, not that a
    live route may be changed. Direct is an explicit outbound, not a subscription
    label or an invitation to automatically fall back to a proxy.
    """
    raw = preferences.get("service_route_modes", {})
    if not isinstance(raw, dict):
        raise ValueError("service_route_modes 线路模式必须是对象")
    allowed = service_ids(preferences)
    profiles = preferences.get("service_profile_bindings") or {}
    nodes = preferences.get("service_node_bindings") or {}
    pools = preferences.get("service_node_pools") or {}
    result = {}
    for service, mode in raw.items():
        if not isinstance(service, str) or mode not in ("default", "direct"):
            raise ValueError("service_route_modes 线路模式只支持 default（跟随默认）或 direct（直连）")
        if service not in allowed:
            continue
        if ((isinstance(profiles, dict) and profiles.get(service))
                or (isinstance(nodes, dict) and nodes.get(service))
                or (isinstance(pools, dict) and pools.get(service))):
            raise ValueError(f"{service} 的跟随默认或直连模式与订阅或节点绑定冲突")
        result[service] = mode
    return result


def validate_outbound_privacy(routes, *, strict_privacy: bool) -> None:
    """An explicit bypass must never silently downgrade a fail-closed policy."""
    if strict_privacy and "DIRECT" in routes:
        raise ValueError("直连目标与严格隐私模式冲突：请将目标改为代理线路，或先关闭该设备的严格隐私模式；未自动修改隐私设置。")


def node_pools(preferences: dict) -> dict[str, list[str]]:
    """Validate ordered explicit candidates; absence alone means full automatic.

    Invalid or empty authority must never be normalized into an unrestricted
    subscription pool. Missing cache entries remain persisted for later refresh.
    """
    raw = preferences.get("service_node_pools", {})
    if not isinstance(raw, dict):
        raise ValueError("service_node_pools 候选节点池必须是对象")
    allowed = service_ids(preferences)
    profiles = preferences.get("service_profile_bindings") or {}
    fixed = preferences.get("service_node_bindings") or {}
    modes = preferences.get("service_route_modes") or {}
    result = {}
    for service, values in raw.items():
        if service not in allowed:
            continue
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_SERVICE_NODE_POOL_SIZE:
            raise ValueError(f"{service} 的候选节点池必须包含 1 至 {MAX_SERVICE_NODE_POOL_SIZE} 个节点")
        keys = []
        for key in values:
            if (not isinstance(key, str) or not key.strip() or len(key) > 128
                    or any(ord(char) < 32 for char in key) or key.strip() in keys):
                raise ValueError(f"{service} 的候选节点标识无效或重复")
            keys.append(key.strip())
        profile_id = profiles.get(service) if isinstance(profiles, dict) else None
        if (not isinstance(profile_id, str) or not profile_id.strip() or len(profile_id) > 64
                or any(ord(char) < 32 for char in profile_id)):
            raise ValueError(f"{service} 的候选节点池没有对应订阅，请重新选择线路")
        if ((isinstance(fixed, dict) and fixed.get(service))
                or (isinstance(modes, dict) and modes.get(service))):
            raise ValueError(f"{service} 的候选节点池与固定节点、跟随默认或直连模式冲突")
        result[service] = keys
    return result


def node_pool_warnings(preferences: dict, *, profile_id: str = "", active_only: bool = False) -> tuple[str, ...]:
    """Resolve only selected pools, returning non-secret partial-cache notices."""
    pools = node_pools(preferences)
    if active_only:
        # Runtime rebuilds must not be blocked by stale candidates on disabled
        # or entirely overridden targets which emit no outbound at all. Keep
        # their persisted authority untouched; editor validation still checks
        # every saved pool before enabling or changing it.
        blueprint = local_proxy._service_route_blueprint(preferences)
        used_groups = set(blueprint["proxy_domain_routes"].values()) | set(blueprint["proxy_ip_cidr_routes"].values())
        pools = {service: keys for service, keys in pools.items()
                 if blueprint["service_routes"].get(service) in used_groups}
    profiles, caches = {}, {}
    notices = []
    for service, keys in pools.items():
        bound = preferences["service_profile_bindings"][service]
        if profile_id and bound != profile_id:
            continue
        if bound not in profiles:
            profiles[bound] = local_proxy._proxy_subscription_profile_for_route(bound)
            caches[bound] = remote_proxy.load_cached_proxy_subscription(profiles[bound])
        detail = []
        local_proxy._selected_subscription_route_pool(
            profiles[bound], ai_sensitive=False, node_keys=keys,
            cached=caches[bound], warnings=detail,
        )
        notices.extend(f"{local_proxy._local_proxy_service_label(service)}：{notice}" for notice in detail)
    return tuple(notices)


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
                      "service_profile_bindings": "订阅绑定", "service_node_bindings": "节点绑定",
                      "service_route_modes": "线路模式", "service_node_pools": "候选节点池"}
            raise ValueError(f"服务分流的{labels[key]}格式无效")
    normalized = local_proxy._normalize_local_proxy_preferences(preferences)
    normalized["service_profile_bindings"] = (
        local_proxy._service_profile_bindings_authority_value(preferences)
    )
    if set(normalized["service_profile_bindings"]) - service_ids(normalized):
        raise ValueError("订阅绑定指向无效的自定义目标，请先修正域名或 IP")
    normalized["service_node_bindings"] = node_bindings(preferences)
    normalized["service_node_pools"] = node_pools(preferences)
    normalized["service_route_modes"] = route_modes(preferences)
    if set(normalized["service_route_modes"]) - service_ids(normalized):
        raise ValueError("线路模式指向无效的自定义目标，请先修正域名或 IP")
    return route_snapshot(normalized)


def config_options(preferences: dict) -> dict:
    options = local_proxy._routing_options_from_preferences(preferences)
    # Scope/strict-privacy are owned by each existing deployment's settings.
    options.pop("strict_privacy", None)
    options.pop("proxy_non_cn", None)
    return {**options, **local_proxy._resolve_service_subscription_routes(preferences),
            "service_route_preferences": normalize_routes(preferences)}


def _rules_digest(rules) -> str:
    return hashlib.sha256(json.dumps(rules, ensure_ascii=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def route_snapshot_marker(preferences: dict, rules: list[str]) -> str:
    """Recoverable intent, never subscription credentials or node definitions.

    The digest detects stale comments after manual rule edits; it is not a
    signature, encryption, or proof of who last edited the remote file.
    """
    payload = {"routes": normalize_routes(preferences), "rules_sha256": _rules_digest(rules)}
    payload["snapshot_sha256"] = _rules_digest([payload["routes"], payload["rules_sha256"]])
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_ROUTE_SNAPSHOT_BYTES:
        raise ValueError("服务分流恢复记录过大，请减少自定义目标")
    return ROUTE_SNAPSHOT_MARKER + base64.urlsafe_b64encode(raw).decode("ascii")


def _managed_route_document(content: str) -> dict:
    if (not isinstance(content, str) or len(content) > 2 * 1024 * 1024
            or remote_proxy.AI_PROXY_CONFIG_MARKER not in content.splitlines()):
        raise ValueError("远端不是可识别的受管配置，已停止覆盖线路")
    try:
        parsed = remote_proxy.yaml.safe_load(content)
    except Exception as exc:
        raise ValueError("远端配置无法解析，已停止覆盖线路") from exc
    if (not isinstance(parsed, dict) or not isinstance(parsed.get("rules"), list)
            or not parsed["rules"] or any(not isinstance(rule, str) for rule in parsed["rules"])):
        raise ValueError("远端分流规则无效，已停止覆盖线路")
    return parsed


def recover_routes_from_config(content: str) -> dict:
    """Read a bounded, self-consistent snapshot; does not access subscriptions."""
    parsed = _managed_route_document(content)
    lines = [line for line in content.splitlines() if line.startswith(ROUTE_SNAPSHOT_MARKER)]
    if not lines:
        raise ValueError("远端没有新版分流恢复记录；旧配置无法可靠还原订阅绑定，请核对后手动设置。远端未修改。")
    if len(lines) != 1 or len(lines[0]) > MAX_ROUTE_SNAPSHOT_BYTES * 4 // 3 + 128:
        raise ValueError("远端分流恢复记录重复或过大，未恢复")
    try:
        raw = base64.b64decode(lines[0][len(ROUTE_SNAPSHOT_MARKER):], altchars=b"-_", validate=True)
        if len(raw) > MAX_ROUTE_SNAPSHOT_BYTES:
            raise ValueError("oversized snapshot")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate snapshot field")
                result[key] = value
            return result
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(payload, dict) or set(payload) != {"routes", "rules_sha256", "snapshot_sha256"}:
            raise ValueError("invalid snapshot shape")
        routes = normalize_routes(payload["routes"])
        if (payload["routes"] != routes or payload["rules_sha256"] != _rules_digest(parsed["rules"])
                or payload["snapshot_sha256"] != _rules_digest([payload["routes"], payload["rules_sha256"]])):
            raise ValueError("snapshot does not match rules")
    except (binascii.Error, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError("远端恢复记录损坏或与现行规则不一致，未恢复；请核对后手动设置") from exc
    return routes


def _remote_has_route_authority(content: str) -> bool:
    if not content.strip():
        return False
    parsed = _managed_route_document(content)
    # Even disabled/manual-default choices need their metadata preserved.
    if ROUTE_SNAPSHOT_MARKER.rstrip() in content and any(recover_routes_from_config(content).values()):
        return True
    if "API-SWITCHER-SUB-" in content:
        return True
    baseline = {f"DOMAIN-SUFFIX,{domain},AI-PROXY" for domain in remote_proxy.AI_PROXY_DOMAINS}
    baseline.update(remote_proxy.PRIVATE_DIRECT_IP_RULES)
    baseline.update(("GEOIP,CN,DIRECT", "MATCH,AI-PROXY", "MATCH,DIRECT"))
    # Legacy custom targets, direct overrides and extra groups must never be
    # silently replaced by an empty local record during a normal refresh.
    groups = parsed.get("proxy-groups")
    return (any(rule not in baseline for rule in parsed["rules"])
            or not isinstance(groups, list) or len(groups) != 1
            or not isinstance(groups[0], dict) or groups[0].get("name") != "AI-PROXY")


def validate_routes(preferences: dict) -> dict:
    normalized = normalize_routes(preferences)
    # Also validate disabled bindings: selecting an unavailable source must be
    # actionable in the editor, never silently saved as a future broken route.
    for service, profile_id in normalized["service_profile_bindings"].items():
        profile = local_proxy._proxy_subscription_profile_for_route(profile_id)
        local_proxy._selected_subscription_route_pool(
            profile, ai_sensitive=False,
            node_key=normalized["service_node_bindings"].get(service, ""),
            node_keys=normalized["service_node_pools"].get(service, ()),
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
        entry = {"id": profile["id"], "name": profile.get("name") or "未命名订阅", "nodes": [],
                 "network_type": remote_proxy.normalize_proxy_subscription_network_type(profile.get("network_type")),
                 "auto_route_usable": False, "auto_route_candidate_count": 0}
        try:
            cached = remote_proxy.load_cached_proxy_subscription(profile)
            if cached:
                entry["nodes"] = [
                    {"key": remote_proxy.proxy_subscription_node_key(item),
                     "label": str(item.node.get("name") or f"节点 {index}")}
                    for index, item in enumerate(cached.nodes, 1)
                    if not str(item.node.get("dialer-proxy") or "").strip()
                ]
                # Node keys include display names; aliases of one connection
                # are not separate fallback exits. Count only independent,
                # normalized connections using the already loaded cache.
                connection_keys = set()
                for item in cached.nodes:
                    if str(item.node.get("dialer-proxy") or "").strip():
                        continue
                    try:
                        connection_keys.add(remote_proxy._proxy_node_connection_key(item.node))
                    except (TypeError, ValueError):
                        continue
                entry["auto_route_candidate_count"] = len(connection_keys)
                # Deployment uses the saved primary, or the first original
                # node when it disappeared; never the first filtered node.
                if cached.nodes:
                    primary = next((item for item in cached.nodes if remote_proxy.proxy_subscription_node_key(item)
                                    == profile.get("selected_node_key")), cached.nodes[0])
                    entry["auto_route_usable"] = not bool(str(primary.node.get("dialer-proxy") or "").strip())
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


@contextmanager
def ssh_profile_route_transaction(previous_name: str | None, new_name: str | None):
    """Move/remove local route authority together with SSH profile metadata.

    Lock order is bindings -> sorted hosts -> profile store, matching route
    application. No SSH connection or live server configuration is changed.
    """
    names = sorted({name for name in (previous_name, new_name) if name})
    with _BINDINGS_LOCK, ExitStack() as locks:
        for name in names:
            locks.enter_context(host_lock(name))
        if not previous_name or previous_name == new_name:
            yield
            return
        source = _host_path(previous_name)
        destination = _host_path(new_name) if new_name else None
        if destination is not None and destination.exists():
            raise ValueError("新 SSH 名称已有分流配置，请换一个名称或先处理原绑定")
        try:
            original = source.read_bytes()
        except FileNotFoundError:
            yield
            return
        try:
            if destination is not None:
                load_ssh_routes(previous_name)  # Fail closed on corrupt authority.
                data = json.loads(original)
                data["ssh_name"] = new_name
                atomic_write_text(destination, json.dumps(data, ensure_ascii=False, indent=2))
            source.unlink()
            yield
        except Exception as original_error:
            errors = []
            try:
                atomic_write_bytes(source, original)
            except Exception as error:
                errors.append(f"恢复旧绑定失败: {error}")
            if destination is not None:
                try:
                    destination.unlink(missing_ok=True)
                except Exception as error:
                    errors.append(f"清理新绑定失败: {error}")
            if errors:
                raise RuntimeError("SSH Profile 操作失败，分流回滚不完整: " + "；".join(errors)) from original_error
            raise


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
    normalized = normalize_routes(preferences)
    atomic_write_text(_host_path(ssh_name), json.dumps(
        {"ssh_name": ssh_name, "routes": normalized},
        ensure_ascii=False, indent=2,
    ))


def load_ssh_route_editor_preferences(ssh_name: str) -> dict:
    with host_lock(ssh_name):
        return {**load_ssh_routes(ssh_name), "_authority_missing": not _host_path(ssh_name).exists()}


@serialized_binding_change
@serialized_ssh_route_operation
def recover_ssh_routes(ssh_name: str) -> dict:
    """Restore missing local intent only; never reload/write the remote proxy."""
    if _host_path(ssh_name).exists():
        raise ValueError("本机已有该服务器的分流记录，未覆盖；请重新打开编辑器")
    content = remote_proxy.read_managed_ai_proxy_config(ssh_name)
    routes = recover_routes_from_config(content)
    if _host_path(ssh_name).exists():
        raise ValueError("本机分流记录已被其他操作创建，未覆盖；请重新打开编辑器")
    _save_ssh_routes(ssh_name, routes)
    return routes


def ssh_config_options(ssh_name: str, old_config: str = "", override=None) -> dict:
    if override is None and not _host_path(ssh_name).exists() and _remote_has_route_authority(old_config):
        raise RuntimeError(f"{ssh_name}: 远端已有独立线路或分流规则，但本机没有对应绑定；"
                           "请在“目标分流”恢复本地分流记录，或核对后重新设置。远端配置未覆盖。")
    return config_options(load_ssh_routes(ssh_name) if override is None else override)


def ssh_probe_kwargs(ssh_name: str) -> dict:
    routes = load_ssh_routes(ssh_name)
    explicit = routes["service_profile_bindings"] or "direct" in routes["service_route_modes"].values()
    return {"routing_preferences": routes} if explicit else {}


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
    had_record = _host_path(ssh_name).exists()
    previous = load_ssh_routes(ssh_name)
    if expected is not None and previous != route_snapshot(expected):
        raise RuntimeError(f"{ssh_name}: 线路已被其他操作修改，请重新打开编辑器")
    updated = validate_routes(preferences)
    warnings = node_pool_warnings(updated)
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
                if had_record:
                    _save_ssh_routes(ssh_name, previous)
                else:
                    # Missing authority is not an empty authorized record.
                    # Preserve that distinction after a failed first apply.
                    _host_path(ssh_name).unlink(missing_ok=True)
            except Exception as rollback:
                raise RuntimeError(f"{ssh_name}: 应用线路失败，原绑定回滚也失败: {rollback}") from exc
            raise RuntimeError(f"{ssh_name}: 应用线路失败，已恢复原绑定: {exc}") from exc
    suffix = "；" + "；".join(warnings) if warnings else ""
    return f"{ssh_name}: 目标分流已保存；{message}{suffix}"
