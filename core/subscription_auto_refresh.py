"""Saved-only subscription refresh; no editor input or implicit deployment.

The caller owns the cross-tab subscription-update reservation. All requested
subscriptions are downloaded first; each managed scope is then applied at most
once, retaining the existing default node for independent service-route updates.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math

from core.lazy_imports import LazyModule
from core.proxy_update_result import ProxyUpdateResult, update_result
from core.subscription_refresh_schedule import SubscriptionRefreshSchedule, make_source_key

local_proxy = LazyModule("core.local_proxy")
remote_proxy = LazyModule("core.remote_proxy")
proxy_routing = LazyModule("core.proxy_routing")
_SCHEDULE = SubscriptionRefreshSchedule()
_SOURCE_FIELDS = ("url", "source_path", "source_revision")


def _current_source(profile_id: str, original: dict) -> dict:
    current = (remote_proxy.load_proxy_subscription_state().get("profiles") or {}).get(profile_id)
    if not isinstance(current, dict) or any(
        current.get(key, "") != original.get(key, "") for key in _SOURCE_FIELDS
    ):
        raise RuntimeError("订阅来源在本轮刷新期间已变化，已跳过旧链接")
    return current


def _content_revision(result) -> str:
    # Hash normalized node identities, not fetch timestamps; repeated downloads
    # of the same content must not defeat failed-apply backoff. No keys are logged.
    identities = [remote_proxy.proxy_subscription_node_key(node) for node in result.nodes]
    if not identities:
        raise ValueError("订阅缓存没有可用节点")
    return hashlib.sha256(json.dumps(identities, separators=(",", ":")).encode()).hexdigest()


def _consumer_identity(scope: str, name: str, routes: dict, active_id: str) -> tuple[str, ...]:
    # Saved intent is part of the acknowledgement: changing a pool or routing
    # mode must not inherit the previous intent's already-applied state.
    payload = json.dumps([active_id, routes], ensure_ascii=False, sort_keys=True, default=str)
    return scope, name, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _step(stage: str, result, **context) -> dict:
    typed = result if isinstance(result, ProxyUpdateResult) else update_result(result, "unknown", warning=True)
    return dict(context, stage=stage, outcome=typed.outcome, message=str(typed),
                warning=typed.warning, retryable=typed.retryable)


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


def refresh_saved_subscriptions(scope: str, *, server_names=(), interval_seconds=None, schedule=None) -> dict:
    if scope not in {"local", "ssh"}:
        raise ValueError("未知订阅刷新范围")
    names = saved_server_names(server_names)
    if scope == "ssh" and not names:
        raise ValueError("未保存 SSH 定时刷新目标，请勾选服务器后重新开启定时热更新")
    scheduled = interval_seconds is not None
    if scheduled:
        if isinstance(interval_seconds, bool):
            raise ValueError("无效的定时刷新间隔")
        interval_seconds = float(interval_seconds)
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("无效的定时刷新间隔")
    scheduler = _SCHEDULE if schedule is None else schedule
    state = remote_proxy.load_proxy_subscription_state()
    profiles = copy.deepcopy(state.get("profiles") or {})
    active_id = str(state.get("active_profile_id") or "")
    errors = []
    scopes = {}
    route_snapshots = {}
    if scope == "local":
        route_snapshots["local"] = local_proxy._load_local_proxy_routing_preferences_strict()
        scopes["local"] = _binding_ids(route_snapshots["local"])
    else:
        for name in names:
            try:
                route_snapshots[name] = proxy_routing.load_ssh_routes(name)
                scopes[name] = _binding_ids(route_snapshots[name])
            except Exception as exc:
                errors.append(f"{name}: 无法读取已保存分流，已跳过：{exc}")
    untracked_retry = bool(errors)
    bound_ids = set().union(*scopes.values()) if scopes else set()
    profile_ids = list(dict.fromkeys([active_id, *sorted(bound_ids)]))
    original_keys = _cached_node_keys(profiles.get(active_id) or {}) if active_id else set()
    results = {}
    apply_messages = []
    steps = []
    ready = {name: set() for name in scopes}
    consumers = {name: _consumer_identity(scope, name, routes, active_id)
                 for name, routes in route_snapshots.items()}
    contexts = {}
    revisions = {}
    counts = {"downloaded_count": 0, "reused_count": 0, "waiting_count": 0}
    allow_direct = local_proxy.local_proxy_subscription_direct_fallback_allowed()

    def cache_decision(key, name):
        decision = scheduler.begin(key, consumers[name], interval_seconds)
        if decision.action == "fetch":
            # Very short injected intervals or a long consumer loop can cross
            # a deadline. This phase only consumes this round's cache; leave
            # the next fetch for the next task, never leak its reservation.
            scheduler.cancel(decision.token)
        return decision

    for profile_id in profile_ids:
        profile = profiles.get(profile_id)
        if not isinstance(profile, dict):
            if profile_id:
                errors.append("已绑定订阅不存在，已保留原运行配置")
                untracked_retry = True
            continue
        url = str(profile.get("url") or "").strip()
        if not url:
            continue  # Imported local YAML is not a downloadable subscription.
        interested = [name for name, bound in scopes.items() if profile_id == active_id or profile_id in bound]
        if not interested:
            continue
        key = make_source_key(profile_id, url=url, source_path=profile.get("source_path", ""),
                              source_revision=profile.get("source_revision", ""),
                              privacy_policy={"allow_direct_fallback": allow_direct})
        contexts[profile_id] = (key, interested)
        token = None
        try:
            current = _current_source(profile_id, profile)
            decisions = {}
            if scheduled:
                for name in interested:
                    decision = scheduler.begin(key, consumers[name], interval_seconds)
                    decisions[name] = decision
                    if decision.action == "fetch":
                        token = decision.token
                        break  # Finish this reservation before considering other consumers.
            if not scheduled or token is not None:
                fetched = remote_proxy.fetch_proxy_subscription(
                    url, profile_id=profile_id, activate=False,
                    allow_direct_fallback=allow_direct,
                    recovery_proxy_provider=local_proxy.local_proxy_subscription_recovery_session,
                )
                _current_source(profile_id, profile)
                results[profile_id] = fetched
                counts["downloaded_count"] += 1
                if scheduled:
                    revision = _content_revision(fetched)
                    version = scheduler.finish_success(token, revision)
                    token = None
                    if version is None:
                        raise RuntimeError("本轮刷新已取消，未采用过期结果")
                    revisions[profile_id] = (revision, version)
                    decisions = {name: cache_decision(key, name)
                                 for name in interested}
                steps.append(_step("download", update_result("订阅已下载并保存缓存", "applied"), profile_id=profile_id))
            elif any(item.action == "cached" for item in decisions.values()):
                cached = remote_proxy.load_cached_proxy_subscription(current)
                revision = _content_revision(cached) if cached else ""
                matching = [item for item in decisions.values()
                            if item.action == "cached" and item.revision == revision]
                if not matching:
                    scheduler.invalidate(key)
                    raise RuntimeError("共享缓存已变化或不可读，稍后重新下载；未应用旧结果")
                _current_source(profile_id, profile)
                results[profile_id] = cached
                revisions[profile_id] = (revision, matching[0].version)
                counts["reused_count"] += 1
                steps.append(_step("cache", update_result("复用已下载缓存", "unchanged"), profile_id=profile_id))
            else:
                counts["waiting_count"] += 1
            for name in interested:
                if not scheduled or (decisions.get(name) and decisions[name].action == "cached"
                                     and decisions[name].revision == revisions.get(profile_id, (None,))[0]):
                    ready[name].add(profile_id)
        except Exception as exc:
            failed_fetch = token is not None
            if token is not None:
                scheduler.finish_failure(token)
            results.pop(profile_id, None)
            for available in ready.values():
                available.discard(profile_id)
            name = str(profile.get("name") or "订阅")
            errors.append(f"{name}: 拉取失败，保留已有缓存：{exc}")
            steps.append(_step("download", update_result(errors[-1], "failed", warning=True, retryable=True),
                               profile_id=profile_id))
            if scheduled and failed_fetch:
                # Download failure must not starve a pending runtime retry of
                # an earlier, verified cache. Never adopt arbitrary old disk
                # data: only the scheduler's exact content incarnation qualifies.
                try:
                    pending = {name: cache_decision(key, name)
                               for name in interested}
                    cached = remote_proxy.load_cached_proxy_subscription(_current_source(profile_id, profile))
                    revision = _content_revision(cached) if cached else ""
                    reusable = {name: item for name, item in pending.items()
                                if item.action == "cached" and item.revision == revision}
                    if reusable:
                        results[profile_id] = cached
                        revisions[profile_id] = (revision, next(iter(reusable.values())).version)
                        for name in reusable:
                            ready[name].add(profile_id)
                        counts["reused_count"] += 1
                        steps.append(_step("cache", update_result("下载暂时失败，复用已验证的旧缓存重试线路", "unchanged",
                                                                  warning=True), profile_id=profile_id))
                except Exception:
                    pass  # Original download error remains visible; no stale apply.

    def origin_matches(name, current_key):
        if not current_key:
            return False
        if current_key in original_keys:
            if scheduled and active_id in revisions:
                revision, version = revisions[active_id]
                scheduler.remember_origin(contexts[active_id][0], consumers[name], revision,
                                          version=version, current_key=current_key)
            return True
        if scheduled and active_id in revisions:
            revision, version = revisions[active_id]
            return current_key == scheduler.proven_origin(
                contexts[active_id][0], consumers[name], revision, version=version,
            )
        return False

    for name, previously_bound in scopes.items():
        available = ready[name] & results.keys()
        if not available:
            continue
        message = update_result(f"{name}: 仅刷新订阅缓存；未改变当前默认线路", "skipped", warning=True)
        consumed = set()
        apply_error = False
        try:
            for profile_id in available:
                current = _current_source(profile_id, profiles[profile_id])
                if scheduled:
                    cached = remote_proxy.load_cached_proxy_subscription(current)
                    if cached is None or _content_revision(cached) != revisions[profile_id][0]:
                        scheduler.invalidate(contexts[profile_id][0])
                        raise RuntimeError("订阅缓存已变化，未应用本轮旧结果")
            if scope == "local":
                current_bound = _binding_ids(local_proxy._load_local_proxy_routing_preferences_strict())
                updated_bound = sorted(previously_bound & current_bound & available)
                if updated_bound:
                    consumed = set(updated_bound)
                    profile_id = updated_bound[0]
                    message = local_proxy.refresh_running_local_service_routes_from_subscription(
                        results[profile_id].nodes, profile_id=profile_id,
                    )
                elif active_id in available and active_id not in previously_bound | current_bound:
                    latest_state = remote_proxy.load_proxy_subscription_state()
                    current_key = local_proxy.current_local_ai_proxy_node_key()
                    if latest_state.get("active_profile_id") == active_id and origin_matches(name, current_key):
                        consumed = {active_id}
                        message = local_proxy.refresh_running_local_ai_proxy_from_subscription(
                            results[active_id].nodes, profile_id=active_id,
                            expected_current_key=current_key,
                            expected_source=tuple(profiles[active_id].get(key, "") for key in _SOURCE_FIELDS),
                            quality_results=dict((latest_state.get("profiles") or {}).get(active_id, {}).get("node_qualities") or {}),
                        )
                    else:
                        message = update_result("默认节点归属无法确认或分组已变化，仅刷新缓存，未切换当前线路", "skipped", warning=True)
            else:
                # Recheck under the same host reservation as the existing refresh
                # API, so a removed binding cannot fall through to main-node swap.
                with proxy_routing.host_lock(name):
                    current_bound = _binding_ids(proxy_routing.load_ssh_routes(name))
                    updated_bound = sorted(previously_bound & current_bound & available)
                    profile_id = updated_bound[0] if updated_bound else ""
                    current_key = ""
                    if not profile_id and active_id in available and active_id not in previously_bound | current_bound:
                        latest_state = remote_proxy.load_proxy_subscription_state()
                        if latest_state.get("active_profile_id") == active_id:
                            current_node = remote_proxy._read_remote_managed_proxy_node(name, 7890)
                            current_key = remote_proxy.proxy_node_key(current_node) if current_node else ""
                            if origin_matches(name, current_key):
                                profile_id = active_id
                if profile_id:
                    consumed = set(updated_bound) if updated_bound else {profile_id}
                    # The refresh API owns commit-time locks and provenance.
                    # Never hold a host lock throughout candidate network probes.
                    message = remote_proxy.refresh_running_ai_proxy_from_subscription(
                        name, results[profile_id].nodes, profile_id=profile_id,
                        persist_selection=False,
                        quality_results=dict((profiles.get(profile_id) or {}).get("node_qualities") or {}),
                        **({"expected_current_key": current_key} if current_key else {}),
                        expected_source=tuple(profiles[profile_id].get(key, "") for key in _SOURCE_FIELDS),
                        **({"require_service_binding": True} if updated_bound else {}),
                    )
        except Exception as exc:
            apply_error = True
            untracked_retry = True
            errors.append(f"{name}: 订阅已刷新，运行态更新失败，请复核当前线路：{exc}")
            message = update_result(errors[-1], "failed", retryable=True, warning=True)
        apply_messages.append(message)
        steps.append(_step("apply", message, scope=scope, target=name))
        if scheduled:
            for profile_id in available:
                key = contexts[profile_id][0]
                revision, version = revisions[profile_id]
                if profile_id in consumed and isinstance(message, ProxyUpdateResult) and message.applied:
                    scheduler.mark_applied(key, consumers[name], revision, version=version)
                else:
                    scheduler.defer_apply(key, consumers[name], revision, version=version,
                                          retryable=(apply_error or profile_id in consumed) and bool(getattr(message, "retryable", False)),
                                          interval_seconds=interval_seconds)
    payload = {"results": results, "errors": errors, "apply_messages": apply_messages,
               "steps": steps, "retryable": bool(errors) or any(item["retryable"] for item in steps), **counts}
    if scheduled:
        delays = [scheduler.next_delay(key, consumers[name], interval_seconds)
                  for key, names_for_profile in contexts.values() for name in names_for_profile]
        # Configuration/cache races can invalidate a source. Avoid a hot timer
        # loop, while ordinary per-source retry deadlines remain independent.
        payload["next_delay_seconds"] = max(1, min(delays, default=interval_seconds))
        if errors and payload["next_delay_seconds"] < 1.5:
            payload["next_delay_seconds"] = 60
        if untracked_retry:
            payload["next_delay_seconds"] = min(60, payload["next_delay_seconds"])
    return payload
