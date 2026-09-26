"""Read-only route summaries. Opening or browsing these never applies routing."""
from __future__ import annotations

import customtkinter as ctk

from core import proxy_routing
from core.subscription_routing_policy import preferred_network_type, route_candidate_count
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, font


def route_description(row, preferences, catalog):
    """Describe saved/draft intent, not live connectivity or the current exit IP."""
    service = row["id"]
    bindings = preferences.get("service_profile_bindings") or {}
    nodes = preferences.get("service_node_bindings") or {}
    modes = preferences.get("service_route_modes") or {}
    inherited = (service.startswith("custom:") and not bindings.get(service)
                 and not modes.get(service))
    source = "custom" if inherited else service
    if modes.get(source) == "direct":
        hint = "目标设备直接访问，不经过代理节点；需自身网络可达，严格隐私模式下不可启用。"
        if inherited:
            hint = "继承自定义默认 · " + hint
        if not row["enabled"]:
            hint = "未启用 · 仅保留直连选择，勾选并保存后才生效。"
        return {"profile": "直连（不经过代理）", "node": "无需代理节点", "hint": hint,
                "warning": False, "bound": not inherited, "enabled": bool(row["enabled"]),
                "inherited": inherited, "source_hint": hint}
    profile_id = bindings.get(source, "")
    node_key = nodes.get(source, "")
    pool = (preferences.get("service_node_pools") or {}).get(source, [])
    profile = next((item for item in catalog if item["id"] == profile_id), None)
    node = next((item for item in (profile or {}).get("nodes", []) if item["key"] == node_key), None)
    def clean(value):
        return " ".join(safe_feedback_text(str(value)).split())
    warning = ""
    if profile_id and profile is None:
        warning = "订阅已失效，请重新选择"
    elif node_key and node is None:
        warning = "固定节点已失效，请重新选择"
    elif profile_id and not profile.get("nodes"):
        warning = profile.get("error") or "订阅暂无可用缓存，请先拉取"
    pool_names = {item["key"]: clean(item["label"]) for item in (profile or {}).get("nodes", [])}
    missing = sum(key not in pool_names for key in pool)
    if pool and missing and not warning:
        warning = ("自选候选全部失效，请重新选择；不会扩大候选范围" if missing == len(pool)
                   else f"自选候选缺失 {missing} / {len(pool)} 个；仅在剩余候选中切换，请检查订阅")
    profile_text = clean(profile["name"]) if profile else ("订阅已失效" if profile_id else "默认线路")
    network_type = (profile or {}).get("network_type", "unknown")
    if network_type in {"residential", "datacenter"}:
        profile_text += " · " + ("家宽" if network_type == "residential" else "非家宽")
    node_text = clean(node["label"]) if node else ("固定节点已失效" if node_key else "订阅首选 + 故障切换")
    if not profile_id and not node_key and not pool:
        node_text = "沿用默认节点策略"
        if (preferences.get("service_route_modes") or {}).get(service) == "default":
            profile_text = "默认线路（手动指定）"
    strategy = "固定节点 · 不自动换出口" if node_key else ("自动切换 · 仅限此订阅，备用按服务策略筛选" if profile_id else "跟随默认线路")
    if pool:
        node_text = f"自选 {len(pool)} 个候选：" + " → ".join(pool_names.get(key, "已失效") for key in pool)
        strategy = "按候选优先顺序和服务连通性切换 · 不使用未选节点"
        if len(pool) - missing == 1:
            strategy += " · 当前仅 1 个可用候选，暂无备用"
    if profile_id and not node_key and not pool and profile and not warning:
        if route_candidate_count(profile) == 1:
            node_text = "订阅首选（暂无备用）"
            strategy = "缓存仅 1 个可用节点，暂无备用可切换"
    if inherited:
        strategy = "继承自定义默认 · " + strategy
    if not row["enabled"]:
        strategy = "未启用 · 保留线路选择，不新增专属规则"
    elif network_type in {"residential", "datacenter"}:
        preferred = preferred_network_type(service)
        if preferred and preferred != network_type:
            label = "家宽" if preferred == "residential" else "非家宽"
            strategy += f" · 此目标建议{label}，可手动改选；已保留当前绑定"
    return {
        "profile": profile_text, "node": node_text, "hint": warning or strategy,
        "warning": bool(warning), "bound": bool(bindings.get(service)),
        "enabled": bool(row["enabled"]), "inherited": inherited,
        "source_hint": ("继承“自定义目标默认线路”；其未指定订阅时，再跟随此设备的默认代理。" if inherited else
                        "跟随此设备部署的默认代理；不是订阅页面当前浏览的节点，实际出口以运行态检查为准。") if not bindings.get(service) else "",
    }


def route_changes(originals, drafts, catalog):
    """Preview changed intent, including effects inherited from custom defaults."""
    changes = []
    for scope, draft in drafts.items():
        original = originals[scope]
        if original == draft:
            continue
        before = {row["id"]: row for row in proxy_routing.route_rows(original)}
        after = {row["id"]: row for row in proxy_routing.route_rows(draft)}
        for service in dict.fromkeys([*after, *before]):
            old_row, new_row = before.get(service), after.get(service)
            old = route_description(old_row, original, catalog) if old_row else None
            new = route_description(new_row, draft, catalog) if new_row else None
            def identity(preferences, row):
                profiles, nodes = preferences["service_profile_bindings"], preferences["service_node_bindings"]
                inherited = (service.startswith("custom:") and not profiles.get(service)
                             and not preferences.get("service_route_modes", {}).get(service))
                source = "custom" if inherited else service
                return (row, profiles.get(service), nodes.get(service), profiles.get(source), nodes.get(source),
                        preferences.get("service_route_modes", {}).get(service),
                        tuple(preferences.get("service_node_pools", {}).get(source, [])))
            if identity(original, old_row) == identity(draft, new_row) and old == new:
                continue
            def describe(row, description):
                if row is None:
                    return "未添加"
                enabled = "启用" if row["enabled"] else "未启用（不新增专属规则）"
                return f'{enabled} · {description["profile"]} → {description["node"]}'
            changes.append({"scope": scope, "service": service, "label": (new_row or old_row)["label"],
                            "before": describe(old_row, old), "after": describe(new_row, new),
                            "removed": new_row is None})
    return changes


class ServiceRouteOverview(ctk.CTkFrame):
    """Compact overview; all edits live in the shared draft editor."""

    def __init__(self, master, *, command, inspect_command=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._command = command
        self._enabled = True
        self._rows = {}
        self._show_inactive = False
        self._narrow = None
        self._layout_scale = None
        self._signature = None
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x")
        self._title = ctk.CTkLabel(self._header, text="目标分流", font=font(15, "bold"), text_color=COLORS["text"], anchor="w")
        self._manage = ctk.CTkButton(self._header, text="管理目标分流", width=130,
                                     command=lambda: self._open(""), **button_style("primary", compact=True))
        self._inspect = (ctk.CTkButton(self._header, text="运行状态 / 网址去向", width=158,
                                      command=inspect_command, **button_style("secondary", compact=True))
                         if inspect_command else None)
        self._summary = ctk.CTkLabel(self, text="正在读取已保存线路…", font=font(12),
                                    text_color=COLORS["muted"], anchor="w", justify="left")
        self._summary.pack(fill="x", pady=(4, 8))
        bind_wraplength(self, self._summary, padding=8)
        self._heading = ctk.CTkFrame(self, fg_color=COLORS["surface_alt"], corner_radius=6)
        self._heading.pack(fill="x", pady=(0, 4))
        for column, text in enumerate(("访问目标", "访问线路", "节点策略")):
            self._heading.grid_columnconfigure(column, weight=(2, 3, 4)[column], uniform="route-overview")
            ctk.CTkLabel(self._heading, text=text, font=font(11), text_color=COLORS["muted"], anchor="w").grid(
                row=0, column=column, sticky="ew", padx=10, pady=2)
        self._heading.grid_columnconfigure(3, minsize=76)
        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(fill="x")
        self._more = ctk.CTkButton(self, text="查看未启用目标", command=self._toggle_inactive,
                                   **button_style("secondary", compact=True))
        self._note = ctk.CTkLabel(
            self, text="这里展示已保存配置，不代表实时连通状态。添加目标、启用站点和选择线路后，在编辑窗口统一保存并应用。",
            font=font(11), text_color=COLORS["muted_soft"], anchor="w", justify="left")
        self._note.pack(fill="x", pady=(6, 0))
        bind_wraplength(self, self._note, padding=8)
        self.bind("<Configure>", self._on_resize, add="+")
        self._layout(True)

    def _open(self, service):
        if self._enabled:
            self._command(service)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        state = "normal" if enabled else "disabled"
        self._manage.configure(state=state)
        if self._inspect:
            self._inspect.configure(state=state)
        for row in self._rows.values():
            row["edit"].configure(state=state)

    def set_routes(self, preferences, catalog):
        # Only non-secret presentation data is retained; equal refreshes do not redraw.
        descriptions = [(row, route_description(row, preferences, catalog)) for row in proxy_routing.route_rows(preferences)
                        if row["id"] != "custom" or preferences.get("custom_targets")
                        or (preferences.get("service_profile_bindings") or {}).get("custom")
                        or (preferences.get("service_route_modes") or {}).get("custom")]
        signature = repr(descriptions)
        if signature == self._signature:
            return
        keys = {row["id"] for row, _ in descriptions}
        for key in self._rows.keys() - keys:
            self._rows.pop(key)["tile"].destroy()
        for info, description in descriptions:
            key = info["id"]
            if key not in self._rows:
                self._rows[key] = self._build_row(key)
            row = self._rows[key]
            row["info"], row["description"] = info, description
            row["target"].configure(text=" ".join(safe_feedback_text(info["label"]).split()))
            row["state"].configure(text="继承基准" if key == "custom" else (("默认启用" if info["always"] else "已启用") if info["enabled"] else "未启用"),
                                    text_color=COLORS["muted"] if info["enabled"] else COLORS["muted_soft"])
            row["profile"].configure(text=description["profile"])
            row["node"].configure(text=description["node"])
            row["hint"].configure(text=description["hint"], text_color=COLORS["warning"] if description["warning"] else COLORS["muted_soft"])
        # Keep canonical order even after custom targets are added/removed.
        self._rows = {info["id"]: self._rows[info["id"]] for info, _ in descriptions}
        enabled = sum(info["enabled"] for info, _ in descriptions if info["id"] != "custom")
        bound = sum(desc["bound"] for _, desc in descriptions)
        warnings = sum(desc["warning"] for _, desc in descriptions)
        self._summary.configure(text=f"已保存 · {enabled} 个目标启用 · {bound} 项独立线路"
                                + (f" · {warnings} 项需修复" if warnings else ""),
                                text_color=COLORS["warning"] if warnings else COLORS["muted"])
        self._layout(self._narrow, force=True)
        self._signature = signature

    def _build_row(self, key):
        tile = ctk.CTkFrame(self._body, fg_color=COLORS["surface_alt"], corner_radius=6)
        target_box = ctk.CTkFrame(tile, fg_color="transparent")
        node_box = ctk.CTkFrame(tile, fg_color="transparent")
        row = {"tile": tile, "target_box": target_box, "node_box": node_box}
        for name, parent, size, color in (("target", target_box, 12, "text"), ("state", target_box, 10, "muted"),
                                           ("profile", tile, 12, "text"), ("node", node_box, 12, "text"),
                                           ("hint", node_box, 10, "muted_soft")):
            label = ctk.CTkLabel(parent, text="", font=font(size), text_color=COLORS[color], anchor="w", justify="left",
                                 width=1, height=16 if size < 12 else 20)
            if name != "profile":
                label.pack(fill="x")
            bind_wraplength(label, label, padding=4, min_width=80)
            row[name] = label
        row["edit"] = ctk.CTkButton(tile, text="设置", width=56, command=lambda: self._open(key),
                                     state="normal" if self._enabled else "disabled", **button_style("secondary", compact=True))
        return row

    def _on_resize(self, event):
        # CTkFrame binds Configure to its canvas, so event.widget is not self.
        self._layout(event.width / self._get_widget_scaling() < 700)

    def _layout(self, narrow, *, force=False):
        scale = self._get_widget_scaling()
        if narrow == self._narrow and scale == self._layout_scale and not force:
            return
        self._narrow = narrow
        self._layout_scale = scale
        self._heading.grid_columnconfigure(3, minsize=round(76 * scale))
        self._header.grid_columnconfigure(0, weight=1)
        self._title.grid(row=0, column=0, sticky="w")
        self._manage.grid(row=1 if narrow else 0, column=0 if narrow else 1,
                          sticky="w" if narrow else "e", pady=(6, 0) if narrow else 0)
        if self._inspect:
            self._inspect.grid(row=2 if narrow else 0, column=0 if narrow else 2,
                               sticky="w" if narrow else "e", padx=0 if narrow else (8, 0),
                               pady=(6, 0) if narrow else 0)
        if narrow:
            self._heading.pack_forget()
        else:
            self._heading.pack(fill="x", pady=(0, 4), before=self._body)
        for row in self._rows.values():
            tile = row["tile"]
            for widget in (row["target_box"], row["profile"], row["node_box"], row["edit"]):
                widget.grid_forget()
            for col in range(4):
                tile.grid_columnconfigure(col, weight=0, minsize=0, uniform="")
            if narrow:
                tile.grid_columnconfigure(0, weight=1)
                row["target_box"].grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 0))
                row["edit"].grid(row=0, column=1, sticky="ne", padx=10, pady=8)
                row["profile"].grid(row=1, column=0, columnspan=2, sticky="ew", padx=10)
                row["node_box"].grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
            else:
                for col, weight in enumerate((2, 3, 4)):
                    tile.grid_columnconfigure(col, weight=weight, uniform="route-overview")
                tile.grid_columnconfigure(3, minsize=round(76 * scale))
                for col, name in enumerate(("target_box", "profile", "node_box", "edit")):
                    row[name].grid(row=0, column=col, sticky="ew", padx=10, pady=6)
        self._filter()

    def _toggle_inactive(self):
        self._show_inactive = not self._show_inactive
        self._filter()

    def _filter(self):
        inactive = 0
        for row in self._rows.values():
            row["tile"].pack_forget()
            hidden = not row["info"]["enabled"] and not row["description"]["warning"]
            inactive += int(hidden)
            if self._show_inactive or not hidden:
                row["tile"].pack(fill="x", pady=(0, 4))
        self._more.pack_forget()
        if inactive:
            self._more.configure(text="收起未启用目标" if self._show_inactive else f"查看未启用目标（{inactive}）")
            self._more.pack(anchor="w", pady=(4, 0), before=self._note)
