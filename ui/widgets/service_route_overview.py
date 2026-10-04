"""Read-only route summaries. Opening or browsing these never applies routing."""
from __future__ import annotations

from datetime import datetime, timezone

import customtkinter as ctk

from core import proxy_routing
from core.proxy_route_diagnostics import RouteOverviewSnapshot, SNAPSHOT_TTL, routing_preferences_fingerprint
from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICE_IDS
from core.subscription_routing_policy import (
    ai_route_candidate_count, ai_route_candidate_nodes, preferred_network_type, route_candidate_count,
)
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, font


def _configure_changed(widget, **options):
    """CTk paints even identical options; avoid needless Tcl calls and redraws."""
    changed = {key: value for key, value in options.items() if widget.cget(key) != value}
    if changed:
        widget.configure(**changed)


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
    ai_service = service in LOCAL_PROXY_AI_SERVICE_IDS
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
    node_text = clean(node["label"]) if node else ("固定节点已失效" if node_key else
                "AI 推荐候选 + 故障切换" if ai_service else "订阅首选 + 故障切换")
    if not profile_id and not node_key and not pool:
        node_text = "沿用默认节点策略"
        if (preferences.get("service_route_modes") or {}).get(service) == "default":
            profile_text = "默认线路（手动指定）"
    strategy = "固定节点 · 不自动换节点；不保证固定 IP / 国家" if node_key else (
        f"自动切换 · 仅限此订阅，{'主备' if ai_service else '备用'}按服务策略筛选；未锁定国家"
        if profile_id else "跟随默认线路")
    if pool:
        node_text = f"自选 {len(pool)} 个候选：" + " → ".join(pool_names.get(key, "已失效") for key in pool)
        strategy = "按候选优先顺序和服务连通性切换 · 不使用未选节点；未验证出口国家"
        if len(pool) - missing == 1:
            strategy += " · 当前仅 1 个可用候选，暂无备用"
    if profile_id and not node_key and not pool and profile and not warning:
        count = ai_route_candidate_count(profile) if ai_service else route_candidate_count(profile)
        if ai_service and not count:
            node_text = "无符合 AI 自动筛选的候选"
            warning = "该订阅没有符合 AI 自动筛选的节点（已排除香港及已知不合格节点）；自动应用将停止，请更换订阅或手动设置"
        elif count == 1:
            node_text = "AI 推荐候选（暂无备用）" if ai_service else "订阅首选（暂无备用）"
            strategy = ("AI 筛选后仅 1 个可用候选，暂无备用可切换；未验证真实出口或连通性"
                        if ai_service else "缓存仅 1 个可用节点，暂无备用可切换")
    policy_warning = ""
    if ai_service and profile and not warning and (node_key or pool):
        allowed = {item["key"] for item in ai_route_candidate_nodes(profile)}
        if any(key not in allowed for key in (pool or [node_key])):
            policy_warning = "手动选择包含香港或不符合 AI 自动筛选的候选；已保留原选择，请检查实际连通性"
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
        "profile": profile_text, "node": node_text,
        "hint": warning or (strategy + (" · " + policy_warning if policy_warning else "")),
        "warning": bool(warning or policy_warning), "bound": bool(bindings.get(service)),
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


class _RuntimeOverview:
    """One-shot snapshot expiry only. This widget never reads controllers or SSH."""

    def _init_runtime(self):
        self._runtime_generation = 0
        self._runtime_snapshot = None
        self._runtime_after_id = None
        self._runtime_closed = False

    def invalidate_runtime(self, message="运行状态待检查 · 点击“检查网址去向”读取"):
        self._runtime_generation += 1
        self._runtime_snapshot = None
        if self._runtime_after_id is not None:
            self.after_cancel(self._runtime_after_id)
            self._runtime_after_id = None
        if self._runtime_closed:
            return self._runtime_generation
        _configure_changed(self._runtime_status, text=message, text_color=COLORS["muted"])
        for label in self._runtime_labels().values():
            label.pack_forget()
        return self._runtime_generation

    def begin_runtime_read(self):
        return self.invalidate_runtime("正在读取运行状态；不会测速或切换节点…")

    def set_runtime_summary(self, summary, token):
        if self._runtime_closed or token != self._runtime_generation:
            return False
        if not isinstance(summary, RouteOverviewSnapshot):
            self.invalidate_runtime("本次运行状态未读取完成，请点击“检查网址去向”重试")
            return False
        expected = self.__dict__.get("_runtime_preferences_fingerprint")
        if expected is not None and expected != summary.preferences_fingerprint:
            self.invalidate_runtime("已保存分流发生变化，请重新检查运行状态")
            return False
        if self._runtime_after_id is not None:
            self.after_cancel(self._runtime_after_id)
            self._runtime_after_id = None
        self._runtime_snapshot = summary
        self._prepare_runtime_rows(summary)
        self._render_runtime_summary()
        if not summary.stale():
            remaining = SNAPSHOT_TTL - (datetime.now(timezone.utc) - summary.captured_at).total_seconds()
            self._runtime_after_id = self.after(max(1, int(remaining * 1000) + 1), self._expire_runtime_summary)
        return True

    def _expire_runtime_summary(self):
        self._runtime_after_id = None
        if not self._runtime_closed and self._runtime_snapshot is not None:
            self._render_runtime_summary(expired=True)

    def _render_runtime_summary(self, *, expired=False):
        summary = self._runtime_snapshot
        stale = expired or summary.stale()
        status = f"{summary.scope} · 读取于 {summary.captured_at.astimezone():%H:%M:%S} · "
        status += "快照已过期，请重新检查" if stale else "60 秒内快照，节点仍可能变化"
        if summary.error:
            status += " · " + summary.error
        _configure_changed(self._runtime_status, text=status,
                           text_color=COLORS["warning"] if stale or summary.error else COLORS["muted"])
        labels = self._runtime_labels()
        for item in summary.rows:
            label = labels.get(item.service)
            if label is not None:
                prefix = "历史内核选择（已过期）：" if stale else "内核当前选择（读取时）："
                text = self._runtime_row_text(item, prefix)
                _configure_changed(label, text=text,
                                   text_color=COLORS["warning"] if stale or item.warning else COLORS["muted"])
                if not label.winfo_manager():
                    label.pack(fill="x", pady=(3, 0))

    def _runtime_row_text(self, item, prefix):
        return prefix + item.current

    def _prepare_runtime_rows(self, summary):
        pass

    def _dispose_runtime(self):
        self.invalidate_runtime()
        self._runtime_closed = True


class RuntimeRouteOverview(_RuntimeOverview, ctk.CTkFrame):
    """SSH summary of the one host explicitly inspected by the user."""

    def __init__(self, master, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._init_runtime()
        self._labels = {}
        self._runtime_status = ctk.CTkLabel(
            self, text="未读取运行状态；检查时只连接诊断窗口当前所选的一台服务器。",
            font=font(11), text_color=COLORS["muted"], anchor="w", justify="left", width=1)
        self._runtime_status.pack(fill="x")
        bind_wraplength(self, self._runtime_status, padding=8)
        self._body = ctk.CTkFrame(self, fg_color="transparent")
        self._body.pack(fill="x")
        note = ctk.CTkLabel(self, text="以下每项目标仅核对一个代表域名/IP，不代表真实出口 IP、国家或账号可用性。",
                           font=font(11), text_color=COLORS["muted_soft"], anchor="w", justify="left", width=1)
        note.pack(fill="x", pady=(4, 0))
        bind_wraplength(self, note, padding=8)

    def _runtime_labels(self):
        return self._labels

    def _prepare_runtime_rows(self, summary):
        keys = {item.service for item in summary.rows}
        for key in self._labels.keys() - keys:
            self._labels.pop(key).destroy()
        for item in summary.rows:
            if item.service not in self._labels:
                label = ctk.CTkLabel(self._body, text="", font=font(11), anchor="w", justify="left", width=1)
                bind_wraplength(self._body, label, padding=8)
                self._labels[item.service] = label
        self._labels = {item.service: self._labels[item.service] for item in summary.rows}

    def _runtime_row_text(self, item, prefix):
        return f"{item.label} · 已保存：{item.saved}\n{prefix}{item.current}"

    def destroy(self):
        self._dispose_runtime()
        super().destroy()


class ServiceRouteOverview(_RuntimeOverview, ctk.CTkFrame):
    """Compact overview; all edits live in the shared draft editor."""

    def __init__(self, master, *, command, inspect_command=None, preset_command=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._command = command
        self._preset_command = preset_command
        self._enabled = True
        self._rows = {}
        self._show_inactive = False
        self._narrow = None
        self._layout_scale = None
        self._signature = None
        self._init_runtime()
        self._runtime_preferences_fingerprint = None
        self._header = ctk.CTkFrame(self, fg_color="transparent")
        self._header.pack(fill="x")
        self._title = ctk.CTkLabel(self._header, text="目标分流", font=font(15, "bold"), text_color=COLORS["text"], anchor="w")
        self._header_actions = ctk.CTkFrame(self._header, fg_color="transparent")
        self._header_action_columns = None
        self._preset = (ctk.CTkButton(
            self._header_actions, text="一键套用智能分流方案", width=200,
            command=self._open_preset, **button_style("primary", compact=True))
            if preset_command is not None else None)
        self._manage = ctk.CTkButton(self._header_actions, text="管理目标分流", width=130,
                                     command=lambda: self._open(""), **button_style("secondary", compact=True))
        self._inspect = (ctk.CTkButton(self._header_actions, text="检查网址去向", width=130,
                                      command=inspect_command, **button_style("secondary", compact=True))
                         if inspect_command else None)
        self._header.bind("<Configure>", self._layout_header_actions, add="+")
        self._layout_header_actions()
        self._preset_note = None
        if self._preset is not None:
            self._preset_note = ctk.CTkLabel(
                self, text="AI 优先家宽，普通网站优先非家宽。\n先预览再应用，不会自动修改现有分流。",
                font=font(11), text_color=COLORS["muted"], anchor="w", justify="left")
            self._preset_note.pack(fill="x", pady=(4, 0))
            bind_wraplength(self, self._preset_note, padding=8)
        self._summary = ctk.CTkLabel(self, text="正在读取已保存线路…", font=font(12),
                                    text_color=COLORS["muted"], anchor="w", justify="left")
        self._summary.pack(fill="x", pady=(4, 8))
        bind_wraplength(self, self._summary, padding=8)
        self._runtime_status = ctk.CTkLabel(
            self, text="运行状态待检查 · 点击“检查网址去向”读取", font=font(11),
            text_color=COLORS["muted"], anchor="w", justify="left", width=1)
        if inspect_command:
            self._runtime_status.pack(fill="x", pady=(0, 6))
        bind_wraplength(self, self._runtime_status, padding=8)
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
            self, text="仅核对每个目标的一个代表域名/IP；内核选择不代表真实出口或实时连通。点“检查网址去向”查看详情。"
                       if inspect_command else "这里只展示已保存配置，不代表实时连通；线路修改需在编辑窗口保存并应用。",
            font=font(11), text_color=COLORS["muted_soft"], anchor="w", justify="left")
        self._note.pack(fill="x", pady=(6, 0))
        bind_wraplength(self, self._note, padding=8)
        self.bind("<Configure>", self._on_resize, add="+")
        self._layout(True)

    def _open(self, service):
        if self._enabled:
            self._command(service)

    def _open_preset(self):
        if self._enabled and self._preset_command is not None:
            self._preset_command()

    def set_enabled(self, enabled):
        if self._enabled == bool(enabled):
            return
        self._enabled = bool(enabled)
        if not enabled:
            self.invalidate_runtime("代理操作进行中；完成后可重新检查运行状态")
        state = "normal" if enabled else "disabled"
        if self._preset:
            self._preset.configure(state=state)
        self._manage.configure(state=state)
        if self._inspect:
            self._inspect.configure(state=state)
        for row in self._rows.values():
            row["edit"].configure(state=state)

    def set_routes(self, preferences, catalog):
        # Only non-secret presentation data is retained; equal refreshes do not redraw.
        fingerprint = routing_preferences_fingerprint(preferences)
        if fingerprint != self._runtime_preferences_fingerprint:
            self._runtime_preferences_fingerprint = fingerprint
            self.invalidate_runtime()
        descriptions = [(row, route_description(row, preferences, catalog)) for row in proxy_routing.route_rows(preferences)
                        if row["id"] != "custom" or preferences.get("custom_targets")
                        or (preferences.get("service_profile_bindings") or {}).get("custom")
                        or (preferences.get("service_route_modes") or {}).get("custom")]
        signature = repr(descriptions)
        if signature == self._signature:
            return
        if self._signature is not None:
            self.invalidate_runtime("已保存线路或订阅信息发生变化，请重新检查运行状态")
        keys = {row["id"] for row, _ in descriptions}
        for key in self._rows.keys() - keys:
            self._rows.pop(key)["tile"].destroy()
        for info, description in descriptions:
            key = info["id"]
            if key not in self._rows:
                self._rows[key] = self._build_row(key)
            row = self._rows[key]
            if row.get("info") == info and row.get("description") == description:
                continue
            row["info"], row["description"] = info, description
            _configure_changed(row["target"], text=" ".join(safe_feedback_text(info["label"]).split()))
            _configure_changed(row["state"], text="继承基准" if key == "custom" else (("默认启用" if info["always"] else "已启用") if info["enabled"] else "未启用"),
                                    text_color=COLORS["muted"] if info["enabled"] else COLORS["muted_soft"])
            _configure_changed(row["profile"], text=description["profile"])
            _configure_changed(row["node"], text=description["node"])
            _configure_changed(row["hint"], text=description["hint"], text_color=COLORS["warning"] if description["warning"] else COLORS["muted_soft"])
        # Keep canonical order even after custom targets are added/removed.
        self._rows = {info["id"]: self._rows[info["id"]] for info, _ in descriptions}
        enabled = sum(info["enabled"] for info, _ in descriptions if info["id"] != "custom")
        bound = sum(desc["bound"] for _, desc in descriptions)
        warnings = sum(desc["warning"] for _, desc in descriptions)
        _configure_changed(self._summary, text=f"已保存 · {enabled} 个目标启用 · {bound} 项独立线路"
                                + (f" · {warnings} 项需检查" if warnings else ""),
                                text_color=COLORS["warning"] if warnings else COLORS["muted"])
        self._layout(self._narrow)
        self._filter()
        self._signature = signature

    def _runtime_labels(self):
        return {key: row["runtime"] for key, row in self._rows.items()}

    def destroy(self):
        self._dispose_runtime()
        super().destroy()

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
        row["runtime"] = ctk.CTkLabel(node_box, text="", font=font(10), text_color=COLORS["muted"],
                                       anchor="w", justify="left", width=1, height=16)
        bind_wraplength(row["runtime"], row["runtime"], padding=4, min_width=80)
        row["edit"] = ctk.CTkButton(tile, text="设置", width=56, command=lambda: self._open(key),
                                     state="normal" if self._enabled else "disabled", **button_style("secondary", compact=True))
        return row

    def _on_resize(self, event):
        # CTkFrame binds Configure to its canvas, so event.widget is not self.
        self._layout(event.width / self._get_widget_scaling() < 700)

    def _layout_header_actions(self, event=None):
        # Use the available header width, not the buttons' requested width;
        # otherwise wrapping changes its own breakpoint and can oscillate.
        width = (event.width if event else self._header.winfo_width()) / self._get_widget_scaling()
        if self._preset is not None and self._inspect is not None:
            columns = 3 if width >= 500 else (2 if width >= 280 else 1)
        elif self._preset is not None:
            columns = 2 if width >= 350 else 1
        else:
            columns = 2 if width >= 280 else 1
        if columns == self._header_action_columns:
            return
        self._header_action_columns = columns
        buttons = [button for button in (self._preset, self._manage, self._inspect) if button is not None]
        for button in buttons:
            button.grid_forget()
        for col in range(3):
            self._header_actions.grid_columnconfigure(col, weight=1 if col < columns else 0)
        for index, button in enumerate(buttons):
            if len(buttons) == 3 and columns == 2:
                # Keep the primary action prominent without squeezing its title.
                row, column, span = (0, 0, 2) if index == 0 else (1, index - 1, 1)
            else:
                row, column, span = index // columns, index % columns, 1
            button.grid(row=row, column=column, columnspan=span, sticky="ew",
                        padx=(8, 0) if column else 0, pady=(6, 0) if row else 0)

    def _layout(self, narrow, *, force=False):
        scale = self._get_widget_scaling()
        if narrow != self._narrow or scale != self._layout_scale or force:
            self._narrow = narrow
            self._layout_scale = scale
            self._heading.grid_columnconfigure(3, minsize=round(76 * scale))
            self._header.grid_columnconfigure(0, weight=1)
            self._title.grid(row=0, column=0, sticky="w")
            self._header_actions.grid(row=1 if narrow else 0, column=0 if narrow else 1,
                                      sticky="ew" if narrow else "e", pady=(6, 0) if narrow else 0)
            if narrow:
                self._heading.pack_forget()
            else:
                self._heading.pack(fill="x", pady=(0, 4), before=self._body)
        for row in self._rows.values():
            # New targets need layout even when the viewport has not changed.
            if row.get("layout") == (narrow, scale) and not force:
                continue
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
            row["layout"] = (narrow, scale)

    def _toggle_inactive(self):
        self._show_inactive = not self._show_inactive
        self._filter()

    def _filter(self):
        inactive = 0
        desired = []
        for row in self._rows.values():
            hidden = not row["info"]["enabled"] and not row["description"]["warning"]
            inactive += int(hidden)
            if self._show_inactive or not hidden:
                desired.append(row["tile"])
        # Only insert, remove or move changed targets; stable rows stay mapped.
        current = list(self._body.pack_slaves())
        wanted = set(desired)
        for tile in reversed(current):
            if tile not in wanted:
                tile.pack_forget()
        order = [tile for tile in current if tile in wanted]
        for index, tile in enumerate(desired):
            if index < len(order) and order[index] is tile:
                continue
            if tile in order:
                order.remove(tile)
            options = {"fill": "x", "pady": (0, 4)}
            if index < len(order):
                options["before"] = order[index]
            tile.pack(**options)
            order.insert(index, tile)
        if inactive:
            _configure_changed(self._more, text="收起未启用目标" if self._show_inactive else f"查看未启用目标（{inactive}）")
            if not self._more.winfo_manager():
                self._more.pack(anchor="w", pady=(4, 0), before=self._note)
        elif self._more.winfo_manager():
            self._more.pack_forget()
