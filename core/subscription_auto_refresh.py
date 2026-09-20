"""Saved-only subscription refresh; no editor input or implicit deployment.

The caller owns the cross-tab subscription-update reservation. All requested
subscriptions are downloaded first; each managed scope is then applied at most
once, retaining the existing default node for independent service-route updates.
"""
from __future__ import annotations

import copy

from core.lazy_imports import LazyModule

local_proxy = LazyModule("core.local_proxy")
remote_proxy = LazyModule("core.remote_proxy")
proxy_routing = LazyModule("core.proxy_routing")


def saved_server_names(value) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(
        name.strip() for name in value
        if isinstance(name, str) and name.strip()
        and len(name) <= 256 and not any(ord(char) < 32 for char in name)
    ))


def _binding_ids(routes: dict) -> set[str]:
    return {str(value) for value in (routes.get("service_profile_bindings") or {}).values() if value}


def _cached_node_keys(profile: dict) -> set[str]:
    try:
        cached = remote_proxy.load_cached_proxy_subscription(profile)
        return {remote_proxy.proxy_subscription_node_key(node) for node in cached.nodes} if cached else set()
    except Exception:
        # Without old-cache evidence, a background refresh cannot safely assume
        # that a newly selected subscription owns the existing default node.
        return set()


def refresh_saved_subscriptions(scope: str, *, server_names=()) -> dict:
    if scope not in {"local", "ssh"}:
        raise ValueError("未知订阅刷新范围")
    names = saved_server_names(server_names)
    if scope == "ssh" and not names:
        raise ValueError("未保存 SSH 定时刷新目标，请勾选服务器后重新开启定时热更新")
    state = remote_proxy.load_proxy_subscription_state()
    profiles = copy.deepcopy(state.get("profiles") or {})
    active_id = str(state.get("active_profile_id") or "")
    errors = []
    scopes = {}
    if scope == "local":
        scopes["local"] = _binding_ids(local_proxy._load_local_proxy_routing_preferences_strict())
    else:
        for name in names:
            try:
                scopes[name] = _binding_ids(proxy_routing.load_ssh_routes(name))
            except Exception as exc:
                errors.append(f"{name}: 无法读取已保存分流，已跳过：{exc}")
    bound_ids = set().union(*scopes.values()) if scopes else set()
    profile_ids = list(dict.fromkeys([active_id, *sorted(bound_ids)]))
    original_keys = _cached_node_keys(profiles.get(active_id) or {}) if active_id else set()
    results = {}
    apply_messages = []
    allow_direct = local_proxy.local_proxy_subscription_direct_fallback_allowed()
    for profile_id in profile_ids:
        profile = profiles.get(profile_id)
        if not isinstance(profile, dict):
            if profile_id:
                errors.append("已绑定订阅不存在，已保留原运行配置")
            continue
        url = str(profile.get("url") or "").strip()
        if not url:
            continue  # Imported local YAML is not a downloadable subscription.
        try:
            current = (remote_proxy.load_proxy_subscription_state().get("profiles") or {}).get(profile_id)
            if not isinstance(current, dict) or any(
                current.get(key, "") != profile.get(key, "")
                for key in ("url", "source_path", "source_revision")
            ):
                raise RuntimeError("订阅来源在本轮刷新期间已变化，已跳过旧链接")
            results[profile_id] = remote_proxy.fetch_proxy_subscription(
                url, profile_id=profile_id, activate=False,
                allow_direct_fallback=allow_direct,
                recovery_proxy_provider=local_proxy.local_proxy_subscription_recovery_session,
            )
        except Exception as exc:
            name = str(profile.get("name") or "订阅")
            errors.append(f"{name}: 拉取失败，保留已有缓存：{exc}")

    for name, previously_bound in scopes.items():
        try:
            if scope == "local":
                current_bound = _binding_ids(local_proxy._load_local_proxy_routing_preferences_strict())
                updated_bound = sorted(previously_bound & current_bound & results.keys())
                if updated_bound:
                    profile_id = updated_bound[0]
                    apply_messages.append(local_proxy.refresh_running_local_service_routes_from_subscription(
                        results[profile_id].nodes, profile_id=profile_id,
                    ))
                elif active_id in results and active_id not in previously_bound:
                    latest_state = remote_proxy.load_proxy_subscription_state()
                    current_key = local_proxy.current_local_ai_proxy_node_key()
                    if latest_state.get("active_profile_id") == active_id and current_key in original_keys:
                        apply_messages.append(local_proxy.refresh_running_local_ai_proxy_from_subscription(
                            results[active_id].nodes, profile_id=active_id,
                            expected_current_key=current_key,
                            quality_results=dict((latest_state.get("profiles") or {}).get(active_id, {}).get("node_qualities") or {}),
                        ))
                    else:
                        apply_messages.append("默认节点归属无法确认或分组已变化，仅刷新缓存，未切换当前线路")
            else:
                # Recheck under the same host reservation as the existing refresh
                # API, so a removed binding cannot fall through to main-node swap.
                with proxy_routing.host_lock(name):
                    current_bound = _binding_ids(proxy_routing.load_ssh_routes(name))
                    updated_bound = sorted(previously_bound & current_bound & results.keys())
                    profile_id = updated_bound[0] if updated_bound else ""
                    if not profile_id and active_id in results and active_id not in previously_bound:
                        latest_state = remote_proxy.load_proxy_subscription_state()
                        if latest_state.get("active_profile_id") == active_id and original_keys:
                            current_node = remote_proxy._read_remote_managed_proxy_node(name, 7890)
                            current_key = remote_proxy.proxy_node_key(current_node) if current_node else ""
                            if current_key in original_keys:
                                profile_id = active_id
                    if profile_id:
                        # Do not overwrite per-host strict-privacy choices, nor
                        # persist a shared selection from one host onto another.
                        apply_messages.append(remote_proxy.refresh_running_ai_proxy_from_subscription(
                            name, results[profile_id].nodes, profile_id=profile_id,
                            persist_selection=False,
                            quality_results=dict((profiles.get(profile_id) or {}).get("node_qualities") or {}),
                        ))
                    elif results:
                        apply_messages.append(f"{name}: 仅刷新订阅缓存；未改变当前默认线路")
        except Exception as exc:
            errors.append(f"{name}: 订阅已刷新，运行态更新失败，保留原线路：{exc}")
    return {"results": results, "errors": errors, "apply_messages": apply_messages}
