"""Pure editor-draft defaults from user-assigned subscription labels.

Labels describe the user's intent, not a verified property of an exit IP. This
module neither reads caches nor applies routing; existing/manual choices win.
Drafts still require an explicit save/apply before any live route can change.
"""
from __future__ import annotations

import copy

from core.local_proxy_constants import LOCAL_PROXY_BUILTIN_SITE_IDS


# Traffic-allocation defaults, not claims that these services require a
# residential IP. Unknown/custom services are deliberately never inferred.
# Reserve the residential default for AI; ordinary browsing (including social
# sites) uses datacenter subscriptions unless the user deliberately overrides it.
_TARGETS = (
    ("residential", "家宽", (("openai", "OpenAI / Codex"),
                            ("claude", "Claude Code"),
                            ("google_ai", "Google AI / Gemini"))),
    ("datacenter", "非家宽", (("youtube", "YouTube"),
                           ("google", "Google 搜索/账号"),
                           ("github", "GitHub"),
                           ("huggingface", "Hugging Face"),
                           ("x_twitter", "X / Twitter"),
                           ("reddit", "Reddit"),
                           ("discord", "Discord"),
                           ("telegram", "Telegram"))),
)


def preferred_network_type(service: str) -> str:
    """Return the default label for a known service; never inspect an IP/name."""
    return next((network_type for network_type, _label, targets in _TARGETS
                 if any(service == key for key, _name in targets)), "")


def route_candidate_count(profile: dict) -> int:
    """Count distinct cached candidates, not a promise of live/AI availability."""
    count = profile.get("auto_route_candidate_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        return count
    nodes = profile.get("nodes")
    if not isinstance(nodes, (list, tuple)):
        return 0
    return len({node["key"] for node in nodes
                if isinstance(node, dict) and isinstance(node.get("key"), str) and node["key"]})


def _unavailable_reason(profile: dict) -> str:
    if not isinstance(profile.get("id"), str) or not profile["id"].strip():
        return "订阅标识无效"
    nodes = profile.get("nodes")
    if not isinstance(nodes, (list, tuple)):
        return "无可用节点缓存"
    node_keys = {
        node["key"] for node in nodes
        if isinstance(node, dict) and isinstance(node.get("key"), str) and node["key"]
    }
    if not node_keys or not route_candidate_count(profile):
        return "无可用节点缓存"
    if "auto_route_usable" in profile:
        # The catalog checks the actual primary before filtering chained nodes.
        # A known-good result also covers a stale saved key whose safe fallback
        # is the first raw subscription node, matching deployment semantics.
        if profile["auto_route_usable"] is not True:
            return "无可独立使用的首选节点"
    else:
        selected = profile.get("selected_node_key")
        if selected and (not isinstance(selected, str) or selected not in node_keys):
            return "首选节点不在可用节点列表"
    return ""


def suggest_tagged_routes(
    preferences: dict, catalog: list[dict], protected_services=(),
) -> tuple[dict, list[str]]:
    """Return an independent draft and human-readable explanations.

    Only unassigned, not explicitly disabled targets receive a unique source.
    No name-based inference, cross-label fallback, node pinning, or live writes
    are performed. ``protected_services`` preserves edits such as explicitly
    choosing "follow default" before saving. Persisted ``service_route_modes``
    protects the same choice after the editor has been closed and reopened.
    """
    if not isinstance(preferences, dict):
        raise ValueError("服务分流草稿必须是对象")
    draft = copy.deepcopy(preferences)
    for key in ("service_profile_bindings", "service_node_bindings", "builtin_sites", "service_route_modes", "service_node_pools"):
        if key in draft and not isinstance(draft[key], dict):
            return draft, ["服务分流草稿格式无效，未自动分配；请先修正已有配置。"]
    profiles = draft.get("service_profile_bindings", {})
    nodes = draft.get("service_node_bindings", {})
    sites = draft.get("builtin_sites", {})
    modes = draft.get("service_route_modes", {})
    pools = draft.get("service_node_pools", {})
    if any(not isinstance(service, str) or mode != "default" for service, mode in modes.items()):
        return draft, ["服务分流线路模式无效，未自动分配；请先修正已有配置。"]
    protected = ({protected_services} if isinstance(protected_services, str)
                 else set(protected_services or ()))
    protected.update(modes)
    catalog = [item for item in catalog if isinstance(item, dict)]
    notices = []
    kept = []
    for network_type, type_label, targets in _TARGETS:
        pending = []
        for service, label in targets:
            # Even invalid/stale or deliberately disabled bindings remain the
            # user's authority. Suggestions must not repair them silently.
            disabled = (service in LOCAL_PROXY_BUILTIN_SITE_IDS
                        and service in sites and sites[service] is not True)
            if service in profiles or service in nodes or service in pools or service in protected or disabled:
                kept.append(label)
            else:
                pending.append((service, label))
        if not pending:
            continue
        target_label = "、".join(label for _service, label in pending)
        tagged = [item for item in catalog if item.get("network_type") == network_type]
        if not tagged:
            notices.append(f"{target_label} 未分配：未找到标记为{type_label}的订阅，请先设置订阅标记。")
            continue
        eligible = {}
        reasons = {}
        for profile in tagged:
            reason = _unavailable_reason(profile)
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                eligible[profile["id"]] = profile
        if not eligible:
            detail = "；".join(f"{count} 个{reason}" for reason, count in reasons.items())
            notices.append(f"{target_label} 未分配：{type_label}订阅暂不可用（{detail}）。请先拉取缓存或选择独立节点。")
            continue
        if len(eligible) != 1:
            notices.append(f"{target_label} 未分配：存在 {len(eligible)} 个可用{type_label}订阅，请手动选择；不会猜选。")
            continue
        profile_id = next(iter(eligible))
        profiles = draft.setdefault("service_profile_bindings", {})
        for service, _label in pending:
            profiles[service] = profile_id
            if service in LOCAL_PROXY_BUILTIN_SITE_IDS:
                draft.setdefault("builtin_sites", {})[service] = True
        strategy = ("使用订阅首选与同订阅故障切换（备用按服务策略筛选）" if route_candidate_count(eligible[profile_id]) > 1
                    else "使用订阅首选；缓存仅 1 个可用节点，暂无备用可切换")
        notices.append(f"已为{target_label}填入{type_label}订阅，{strategy}；仅修改草稿，保存并应用后生效。")
    if kept:
        notices.append(f"已保留{ '、'.join(kept) }的已有线路或手动修改，未覆盖节点及启用状态。")
    return draft, notices


_LEGACY_SOCIAL_TARGETS = (("x_twitter", "X / Twitter"), ("reddit", "Reddit"))


def _legacy_cleanup_authority_valid(preferences: dict) -> bool:
    """Cleanup is not a configuration repair path; ambiguous authority wins."""
    fields = ("service_profile_bindings", "service_node_bindings", "builtin_sites",
              "service_route_modes", "service_node_pools")
    if any(key in preferences and not isinstance(preferences[key], dict) for key in fields):
        return False
    if any(not isinstance(service, str) for key in fields for service in preferences.get(key, {})):
        return False
    for key, limit in (("service_profile_bindings", 64), ("service_node_bindings", 128)):
        if any(not isinstance(value, str) or len(value) > limit or value != value.strip()
               or any(ord(char) < 32 for char in value) for value in preferences.get(key, {}).values()):
            return False
    if any(not isinstance(value, bool) for value in preferences.get("builtin_sites", {}).values()):
        return False
    profiles = preferences.get("service_profile_bindings", {})
    pins = preferences.get("service_node_bindings", {})
    modes = preferences.get("service_route_modes", {})
    pools = preferences.get("service_node_pools", {})
    for service, mode in modes.items():
        if mode != "default" or profiles.get(service) or pins.get(service) or pools.get(service):
            return False
    for service, keys in pools.items():
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 16 or not profiles.get(service)
                or pins.get(service) or modes.get(service)):
            return False
        seen = set()
        for key in keys:
            if (not isinstance(key, str) or not key or key != key.strip() or len(key) > 128
                    or any(ord(char) < 32 for char in key) or key in seen):
                return False
            seen.add(key)
    if any(value and not profiles.get(service) for service, value in pins.items()):
        return False
    return True


def _legacy_catalog_groups(catalog) -> dict[str, list[dict]]:
    groups = {}
    for row in catalog if isinstance(catalog, (list, tuple)) else ():
        if not isinstance(row, dict):
            continue
        profile_id = row.get("id")
        if (not isinstance(profile_id, str) or not profile_id or profile_id != profile_id.strip()
                or len(profile_id) > 64 or any(ord(char) < 32 for char in profile_id)):
            continue
        groups.setdefault(profile_id, []).append(row)
    return groups


def legacy_social_route_candidates(
    preferences: dict, catalog: list[dict], protected_services=(),
) -> tuple[str, ...]:
    """Identify old social defaults heuristically, without assuming provenance.

    Detection does not require an available replacement. Explicit node/pool,
    default, disabled and in-editor protected choices are never candidates.
    """
    if not isinstance(preferences, dict):
        raise ValueError("服务分流草稿必须是对象")
    if not _legacy_cleanup_authority_valid(preferences):
        return ()
    protected = ({protected_services} if isinstance(protected_services, str)
                 else set(protected_services or ()))
    profiles = preferences.get("service_profile_bindings", {})
    pins = preferences.get("service_node_bindings", {})
    pools = preferences.get("service_node_pools", {})
    modes = preferences.get("service_route_modes", {})
    sites = preferences.get("builtin_sites", {})
    groups = _legacy_catalog_groups(catalog)
    candidates = []
    for service, _label in _LEGACY_SOCIAL_TARGETS:
        if (service in protected or service in pins or service in pools or service in modes
                or (service in sites and sites[service] is not True)):
            continue
        rows = groups.get(profiles.get(service), ())
        if rows and all(row.get("network_type") == "residential" for row in rows):
            candidates.append(service)
    return tuple(candidates)


def cleanup_legacy_social_routes(
    preferences: dict, catalog: list[dict], protected_services=(),
) -> tuple[dict, list[str]]:
    """Explicit one-click draft cleanup; never called as a live migration.

    Old versions did not persist how a binding was chosen. A residential
    social binding may be intentional; callers must present the heuristic and
    require the existing editor's save/apply action before changing live routes.
    """
    if not isinstance(preferences, dict):
        raise ValueError("服务分流草稿必须是对象")
    draft = copy.deepcopy(preferences)
    notices = ["旧版未记录来源，可能是手动选择；这里只识别旧版 X / Reddit 家宽默认分流，不代表家宽线路无效。"]
    if not _legacy_cleanup_authority_valid(draft):
        return draft, [*notices, "服务分流草稿格式无效，未清理；请先修正已有配置。"]
    candidates = legacy_social_route_candidates(draft, catalog, protected_services)
    if not candidates:
        return draft, [*notices, "没有符合清理条件的旧版社交分流；已有手动策略、保护项及启用状态均保持不变。"]
    eligible = {profile_id for profile_id, rows in _legacy_catalog_groups(catalog).items()
                if all(row.get("network_type") == "datacenter" and not _unavailable_reason(row) for row in rows)}
    labels = "、".join(label for service, label in _LEGACY_SOCIAL_TARGETS if service in candidates)
    if not eligible:
        return draft, [*notices, f"{labels} 未清理：没有可用的非家宽订阅，请先标记并拉取缓存；已保留原分流。"]
    if len(eligible) != 1:
        return draft, [*notices, f"{labels} 未清理：存在 {len(eligible)} 个可用非家宽订阅，请手动选择；已保留原分流。"]
    destination = next(iter(eligible))
    for service in candidates:
        draft["service_profile_bindings"][service] = destination
    notices.append(f"已将{labels}改为唯一可用的非家宽订阅；仅修改草稿，保存并应用后生效，未更改节点策略及启用状态。")
    return draft, notices
