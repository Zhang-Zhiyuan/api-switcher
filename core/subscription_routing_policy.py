"""Pure, opt-in draft suggestions from user-assigned subscription labels.

Labels describe the user's intent, not a verified property of an exit IP. This
module neither reads caches nor applies routing; existing/manual choices win.
"""
from __future__ import annotations

import copy


_TARGETS = (
    ("residential", "家宽", (("openai", "OpenAI / Codex"),
                            ("claude", "Claude Code"),
                            ("google_ai", "Google AI / Gemini"))),
    ("datacenter", "非家宽", (("youtube", "YouTube"),
                           ("google", "Google 搜索/账号"))),
)


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
    if not node_keys:
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

    Only unassigned AI/YouTube/Google targets receive a unique eligible source.
    No name-based inference, cross-label fallback, node pinning, or live writes
    are performed. ``protected_services`` preserves edits such as explicitly
    choosing "follow default", which otherwise has no stored binding.
    """
    if not isinstance(preferences, dict):
        raise ValueError("服务分流草稿必须是对象")
    draft = copy.deepcopy(preferences)
    for key in ("service_profile_bindings", "service_node_bindings", "builtin_sites"):
        if key in draft and not isinstance(draft[key], dict):
            return draft, ["服务分流草稿格式无效，未自动分配；请先修正已有配置。"]
    profiles = draft.get("service_profile_bindings", {})
    nodes = draft.get("service_node_bindings", {})
    protected = ({protected_services} if isinstance(protected_services, str)
                 else set(protected_services or ()))
    catalog = [item for item in catalog if isinstance(item, dict)]
    notices = []
    kept = []
    for network_type, type_label, targets in _TARGETS:
        pending = []
        for service, label in targets:
            # Even invalid/stale or deliberately disabled bindings remain the
            # user's authority. Suggestions must not repair them silently.
            if service in profiles or service in nodes or service in protected:
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
            if network_type == "datacenter":
                draft.setdefault("builtin_sites", {})[service] = True
        notices.append(f"已为{target_label}填入{type_label}订阅，使用订阅首选与故障切换；仅修改草稿，保存并应用后生效。")
    if kept:
        notices.append(f"已保留{ '、'.join(kept) }的已有线路或手动修改，未覆盖节点及启用状态。")
    return draft, notices
