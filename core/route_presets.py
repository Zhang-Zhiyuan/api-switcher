"""Explainable, offline routing presets. Planning never applies live settings."""
from __future__ import annotations

import copy

from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICES, LOCAL_PROXY_BUILTIN_SITES
from core.subscription_routing_policy import (
    auto_route_unavailable_reason, route_candidate_count,
    route_catalog_groups, route_draft_authority_valid,
)


ROUTE_PRESETS = {
    "balanced": {
        "label": "智能均衡（推荐）",
        "description": "AI 服务 → 家宽\n视频、搜索、下载、社交 → 非家宽",
    },
    "ai_only": {
        "label": "AI 家宽 + 网站直连",
        "description": "AI 服务 → 家宽；其他内置网站 → 直连\n仅适合设备自身能访问这些网站的网络。",
    },
    "datacenter": {
        "label": "内置目标全走非家宽",
        "description": "所有内置目标 → 非家宽\nAI 可用性仍受出口质量和服务端策略影响。",
    },
}
NETWORK_LABELS = {"residential": "家宽", "datacenter": "非家宽"}
AI_NODE_STRATEGIES = {"fixed": "固定首选节点（不自动切换）", "auto": "订阅内自动切换（可能跨国家）"}
_BINDING_FIELDS = ("service_profile_bindings", "service_node_bindings", "service_node_pools", "service_route_modes")
_TARGETS = (*LOCAL_PROXY_AI_SERVICES, *LOCAL_PROXY_BUILTIN_SITES)
_AI_IDS = {item["id"] for item in LOCAL_PROXY_AI_SERVICES}


def preset_sources(catalog, network_type):
    """Use only unique, explicitly labelled sources with independently usable cache.

    Ambiguous duplicate IDs are rejected even when their labels agree. Neither
    node names nor the number of nodes prove exit-IP quality or connectivity.
    """
    if network_type not in NETWORK_LABELS:
        return []
    return [rows[0] for _key, rows in sorted(route_catalog_groups(catalog).items())
            if len(rows) == 1 and rows[0].get("network_type") == network_type
            and not auto_route_unavailable_reason(rows[0])]


def _desired_network(service, preset_id):
    return ("datacenter" if preset_id == "datacenter" else
            "residential" if service in _AI_IDS else
            "direct" if preset_id == "ai_only" else "datacenter")


def _choose_source(preferences, catalog, network_type, requested, preset_id):
    eligible = preset_sources(catalog, network_type)
    if requested:
        chosen = next((item for item in eligible if item["id"] == requested), None)
        if chosen is None:
            raise ValueError(f"指定的{NETWORK_LABELS[network_type]}订阅已不可用或标记变化，请重新选择；未修改草稿。")
        return chosen, "使用你指定的订阅"
    if not eligible:
        return None, f"缺少可用的{NETWORK_LABELS[network_type]}订阅；请先标记并拉取缓存，原线路保留"

    usage = {}
    for item in _TARGETS:
        service = item["id"]
        if _desired_network(service, preset_id) != network_type:
            continue
        if service not in _AI_IDS and preferences.get("builtin_sites", {}).get(service) is False:
            continue
        profile = preferences.get("service_profile_bindings", {}).get(service)
        if profile:
            usage[profile] = usage.get(profile, 0) + 1
    # Keep an already-used source first; otherwise prefer actual standby
    # availability. Stable IDs, not labels or input order, break ties so a
    # rename/refresh doesn't oscillate recommendations. No latency is inferred.
    chosen = min(eligible, key=lambda item: (-usage.get(item["id"], 0),
                                           -int(route_candidate_count(item) > 1), item["id"]))
    if len(eligible) == 1:
        reason = "唯一可用的同类订阅"
    elif usage.get(chosen["id"]):
        reason = "优先沿用同类目标已使用的订阅"
    elif route_candidate_count(chosen) > 1:
        reason = "优先选择缓存中有备用候选的订阅；同等条件按稳定标识选择"
    else:
        reason = "同等条件按稳定标识选择；可在上方改选"
    return chosen, reason


def _fixed_primary_key(profile):
    """Pin a known primary, never replace a disappeared saved primary by guessing."""
    nodes = profile.get("nodes") or []
    if not isinstance(nodes, (list, tuple)) or not nodes:
        return ""
    key = profile.get("selected_node_key")
    if key is None or key == "":
        key = nodes[0].get("key") if isinstance(nodes[0], dict) else ""
    if (not isinstance(key, str) or not key or key != key.strip() or len(key) > 128
            or any(ord(char) < 32 for char in key)):
        return ""
    return key if sum(isinstance(item, dict) and item.get("key") == key for item in nodes) == 1 else ""


def plan_route_preset(preferences, catalog, preset_id="balanced", *, sources=None,
                      replace_existing=False, protected_services=(), strict_privacy=False, ai_strategy="auto"):
    """Return an independent draft, per-target reasons and an exact change list.

    Existing default/direct/pin/pool choices and disabled sites win unless the
    user explicitly requests replacement. Disabled and custom targets always
    remain untouched. Unavailable source families retain their previous routes;
    they never silently switch to the other family, default or direct.
    """
    if not isinstance(preset_id, str) or preset_id not in ROUTE_PRESETS:
        raise ValueError("请选择有效的分流预设")
    if not isinstance(preferences, dict) or not route_draft_authority_valid(preferences):
        raise ValueError("现有分流草稿格式无效，请先修正；未生成预设。")
    if not isinstance(replace_existing, bool):
        raise ValueError("替换已有分流选项无效")
    if not isinstance(ai_strategy, str) or ai_strategy not in AI_NODE_STRATEGIES:
        raise ValueError("请选择有效的 AI 节点策略")
    if strict_privacy is True and preset_id == "ai_only":
        raise ValueError("网站直连与当前严格隐私模式冲突，请改用其他预设；不会自动关闭隐私保护。")
    sources = {} if sources is None else sources
    if (not isinstance(sources, dict) or set(sources) - NETWORK_LABELS.keys()
            or any(not isinstance(value, str) for value in sources.values())):
        raise ValueError("预设订阅选择无效")
    protected = {protected_services} if isinstance(protected_services, str) else set(protected_services or ())
    needed = (("residential", "datacenter") if preset_id == "balanced"
              else ("residential",) if preset_id == "ai_only" else ("datacenter",))
    selected, primary_keys = {}, {}
    notices = ["仅生成当前位置草稿；核对后保存并应用。自定义目标和已关闭目标不变。",
               "推荐依据订阅标记与本地缓存，未实时测速；家宽标记不代表已验证出口质量。"]
    for network_type in needed:
        profile, reason = _choose_source(preferences, catalog, network_type, sources.get(network_type, ""), preset_id)
        selected[network_type] = {"id": profile["id"] if profile else "", "reason": reason,
                                  "candidate_count": route_candidate_count(profile) if profile else 0}
        primary_keys[network_type] = _fixed_primary_key(profile) if profile else ""
        notices.append(f"{NETWORK_LABELS[network_type]}：{reason}。")
    notices.append("本次将修改的 AI 目标固定到缓存首选；失效不自动换节点，不保证固定 IP / 国家。" if ai_strategy == "fixed" else
                   "本次将修改的 AI 目标使用订阅自动候选，可能随刷新变化，未限制出口国家；可手动自选候选。")
    draft = copy.deepcopy(preferences)
    decisions, changed = [], []
    for item in _TARGETS:
        service = item["id"]
        desired = _desired_network(service, preset_id)
        decision = {"service": service, "label": item["label"], "desired": desired}
        decisions.append(decision)
        if service not in _AI_IDS and preferences.get("builtin_sites", {}).get(service) is False:
            decision.update(status="kept", reason="保留已关闭目标，不自动启用")
            continue
        if not replace_existing and (service in protected or any(service in preferences.get(field, {}) for field in _BINDING_FIELDS)):
            decision.update(status="kept", reason="保留已有线路或手动修改；勾选重新规划才会替换")
            continue
        profile_id = selected.get(desired, {}).get("id")
        if desired != "direct" and not profile_id:
            decision.update(status="unavailable", reason=selected[desired]["reason"])
            continue
        pin_ai = service in _AI_IDS and ai_strategy == "fixed"
        primary_key = primary_keys.get(desired, "")
        if pin_ai and not primary_key:
            decision.update(status="unavailable", reason="订阅首选节点已失效或不明确，请先手动选择；原线路保留，不猜选其他节点")
            continue
        before = tuple(copy.deepcopy(draft.get(field, {}).get(service)) for field in _BINDING_FIELDS)
        was_enabled = draft.get("builtin_sites", {}).get(service)
        for field in _BINDING_FIELDS:
            if field in draft:
                draft[field].pop(service, None)
        if desired == "direct":
            draft.setdefault("service_route_modes", {})[service] = "direct"
            reason = "设备自身网络直连；不可达时不会自动改走代理"
        else:
            draft.setdefault("service_profile_bindings", {})[service] = profile_id
            if pin_ai:
                draft.setdefault("service_node_bindings", {})[service] = primary_key
                reason = "固定本次订阅首选；失效不自动换节点，不保证供应商维持同一 IP / 国家"
            else:
                reason = ("订阅首选 + 同订阅故障切换（备用按服务策略筛选）" if selected[desired]["candidate_count"] > 1
                          else "订阅首选；缓存仅 1 个候选，暂无备用可切换")
        if service not in _AI_IDS:
            draft.setdefault("builtin_sites", {})[service] = True
        after = tuple(draft.get(field, {}).get(service) for field in _BINDING_FIELDS)
        is_changed = before != after or service not in _AI_IDS and was_enabled is not True
        decision.update(status="changed" if is_changed else "unchanged", reason=reason)
        if is_changed:
            changed.append(service)
    return {"preset": preset_id, "ai_strategy": ai_strategy, "draft": draft, "sources": selected, "decisions": decisions,
            "changed_services": changed, "notices": notices}
