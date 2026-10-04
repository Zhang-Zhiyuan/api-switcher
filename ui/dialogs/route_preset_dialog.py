"""Two-step, draft-only routing setup with explicit before/after review."""
from __future__ import annotations

import copy

import customtkinter as ctk

from core import proxy_routing
from core.local_proxy_constants import LOCAL_PROXY_AI_SERVICE_IDS
from core.route_presets import AI_NODE_STRATEGIES, NETWORK_LABELS, ROUTE_PRESETS, plan_route_preset, preset_sources
from ui.dialogs.modal_dialog import RestoreGrabDialog
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font
from ui.widgets.service_route_overview import route_description


def _configure_changed(widget, **options):
    changed = {key: value for key, value in options.items() if widget.cget(key) != value}
    if changed:
        widget.configure(**changed)


class RoutePresetDialog(RestoreGrabDialog):
    def __init__(self, master, *, scope, preferences, catalog, on_accept,
                 protected_services=(), strict_privacy=False, on_manage_sources=None):
        super().__init__(master)
        self.title("智能分流预设")
        self.geometry("800x740")
        self.minsize(540, 480)
        self.configure(fg_color=COLORS["app_bg"])
        self._preferences = copy.deepcopy(preferences)
        self._catalog = copy.deepcopy(catalog)
        self._protected = set(protected_services)
        self._strict_privacy = strict_privacy
        self._on_accept = on_accept
        self._on_manage_sources = on_manage_sources
        self._plan = None
        self._view = "setup"
        self._reviewed_choices = None
        self._last_choices = None
        self._accept_error = False
        self._options, self._source_combos, self._source_rows = {}, {}, {}
        self._preview_rows, self._before = {}, {}
        self._counts = (0, 0, 0)
        self._auto_ack = ctk.BooleanVar(value=False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())

        # Reserve navigation/actions before the scrollable body. Neither long
        # subscription names nor validation errors may push actions off screen.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=(8, 12))
        self._status = ctk.CTkLabel(footer, text="", font=font(11), anchor="w", justify="left", height=20)
        self._status.pack(fill="x", pady=(0, 6))
        bind_wraplength(footer, self._status, padding=4)
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        actions.pack(fill="x")
        actions.grid_columnconfigure((0, 1), weight=1, uniform="preset-actions")
        self._back_button = ctk.CTkButton(actions, text="取消", command=self._back, **button_style("secondary"))
        self._back_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._accept_button = ctk.CTkButton(actions, text="核对分流", command=self._primary_action,
                                           state="disabled", **button_style("accent"))
        self._accept_button.grid(row=0, column=1, sticky="ew")
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(10, 6))
        ctk.CTkLabel(header, text="智能分流预设", font=font(18, "bold"), anchor="w").pack(fill="x", pady=(0, 6))
        steps = ctk.CTkFrame(header, fg_color="transparent")
        steps.pack(fill="x")
        steps.grid_columnconfigure((0, 1), weight=1, uniform="preset-steps")
        self._setup_tab = ctk.CTkButton(steps, text="1  设置方案", command=lambda: self._show_view("setup"),
                                       **button_style("primary", compact=True))
        self._setup_tab.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self._preview_tab = ctk.CTkButton(steps, text="2  核对分流", command=lambda: self._show_view("preview"),
                                         **button_style("secondary", compact=True))
        self._preview_tab.grid(row=0, column=1, sticky="ew")

        body = self._body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=12)
        self._error_detail = ctk.CTkLabel(body, text="", font=font(12), text_color=COLORS["warning"],
                                         anchor="w", justify="left")
        bind_wraplength(body, self._error_detail, padding=16)
        setup = self._setup = ctk.CTkFrame(body, fg_color="transparent")
        setup.pack(fill="x")
        self._preview = ctk.CTkFrame(body, fg_color="transparent")
        self._pages = {"setup": setup, "preview": self._preview}
        scope_label = ctk.CTkLabel(setup, text="当前位置：" + safe_feedback_text(scope), font=font(12),
                                   text_color=COLORS["muted"], anchor="w", justify="left")
        scope_label.pack(fill="x", padx=6, pady=(4, 10))
        bind_wraplength(setup, scope_label, padding=16)
        self._scheme_labels = {item["label"]: key for key, item in ROUTE_PRESETS.items()}
        self._scheme = ctk.CTkComboBox(setup, values=list(self._scheme_labels), state="readonly",
                                      command=lambda _value: self._refresh(), **combo_style())
        self._scheme.set(ROUTE_PRESETS["balanced"]["label"])
        self._scheme.pack(fill="x", padx=6)
        self._description = ctk.CTkLabel(setup, text="", font=font(12), anchor="w", justify="left", text_color=COLORS["muted"])
        self._description.pack(fill="x", padx=6, pady=(4, 10))
        bind_wraplength(setup, self._description, padding=16)
        self._sources_box = ctk.CTkFrame(setup, fg_color="transparent")
        self._sources_box.pack(fill="x", padx=6)
        for network_type, label in NETWORK_LABELS.items():
            options = {"自动推荐": ""}
            for item in preset_sources(self._catalog, network_type):
                text = " ".join(safe_feedback_text(str(item.get("name") or "未命名订阅")).split())
                base, suffix = text, 2
                while text in options:
                    text = f"{base} ({suffix})"
                    suffix += 1
                options[text] = item["id"]
            self._options[network_type] = options
            card = ctk.CTkFrame(self._sources_box, fg_color=COLORS["surface"])
            card.pack(fill="x", pady=(0, 8))
            ctk.CTkLabel(card, text=label + "订阅", font=font(12, "bold"), anchor="w").pack(fill="x", padx=10, pady=(8, 4))
            combo = ctk.CTkComboBox(card, values=list(options), state="readonly",
                                   command=lambda _value: self._refresh(), **combo_style())
            combo.set("自动推荐")
            combo.pack(fill="x", padx=10)
            hint = ctk.CTkLabel(card, text="", font=font(11), anchor="w", justify="left", text_color=COLORS["muted"])
            hint.pack(fill="x", padx=10, pady=(4, 8))
            bind_wraplength(card, hint, padding=24)
            self._source_combos[network_type] = combo
            self._source_rows[network_type] = (card, hint)
        ctk.CTkLabel(setup, text="AI 节点策略", font=font(12, "bold"), anchor="w").pack(fill="x", padx=6, pady=(4, 4))
        self._ai_strategy_labels = {label: key for key, label in AI_NODE_STRATEGIES.items()}
        self._ai_strategy = ctk.CTkComboBox(setup, values=list(self._ai_strategy_labels), state="readonly",
                                          command=lambda _value: self._refresh(), **combo_style())
        self._ai_strategy.set(AI_NODE_STRATEGIES["fixed"])
        self._ai_strategy.pack(fill="x", padx=6)
        self._ai_hint = ctk.CTkLabel(setup, text="", font=font(11), anchor="w", justify="left", text_color=COLORS["muted"])
        self._ai_hint.pack(fill="x", padx=6, pady=(4, 8))
        bind_wraplength(setup, self._ai_hint, padding=16)
        self._replace = ctk.BooleanVar(value=False)
        self._replace_checkbox = ctk.CTkCheckBox(
            setup, text="重新规划已有分流", variable=self._replace,
            command=self._refresh, font=font(12), checkbox_width=18, checkbox_height=18)
        self._replace_checkbox.pack(fill="x", padx=6, pady=(4, 6))
        note = ctk.CTkLabel(setup, text="默认保留手动线路。重新规划会重设固定节点和候选池；\n"
                            "已关闭目标、自定义和全局代理范围仍不变。\n"
                            "推荐仅依据标记与缓存，不代表实时连通或出口质量。",
                            font=font(11), anchor="w", justify="left", text_color=COLORS["muted"])
        note.pack(fill="x", padx=6)
        bind_wraplength(setup, note, padding=16)
        self._source_help = ctk.CTkLabel(setup, text="", font=font(12), anchor="w", justify="left",
                                        text_color=COLORS["warning"])
        bind_wraplength(setup, self._source_help, padding=16)
        self._manage_sources_button = ctk.CTkButton(setup, text="返回设置订阅标记", command=self._manage_sources,
                                                   **button_style("secondary", compact=True))
        self._preview_note = ctk.CTkLabel(self._preview, text="核对原线路与新线路；写入后仍需保存并应用。",
                                         font=font(12), anchor="w", justify="left", text_color=COLORS["muted"])
        self._preview_note.pack(fill="x", padx=6, pady=(4, 10))
        bind_wraplength(self._preview, self._preview_note, padding=16)
        self._auto_ack_checkbox = ctk.CTkCheckBox(
            self._preview, text="允许 AI 使用订阅自动候选（可能跨国家）", variable=self._auto_ack,
            command=self._update_actions, font=font(12), checkbox_width=18, checkbox_height=18)
        for row in proxy_routing.route_rows(self._preferences):
            if row["id"] == "custom" or row["id"].startswith("custom:"):
                continue
            self._before[row["id"]] = route_description(row, self._preferences, self._catalog)
            tile = ctk.CTkFrame(self._preview, fg_color=COLORS["surface"], corner_radius=6)
            tile.pack(fill="x", padx=6, pady=(0, 6))
            heading = ctk.CTkLabel(tile, text=row["label"], font=font(12, "bold"), anchor="w")
            heading.pack(fill="x", padx=10, pady=(6, 0))
            detail = ctk.CTkLabel(tile, text="", font=font(11), anchor="w", justify="left", height=18)
            detail.pack(fill="x", padx=10, pady=(0, 6))
            bind_wraplength(tile, detail, padding=24)
            self._preview_rows[row["id"]] = (heading, detail)
        self._refresh()
        center_window(self, master)
        self.grab_set()

    def _choices(self):
        return {"preset_id": self._scheme_labels[self._scheme.get()],
                "sources": {key: self._options[key][combo.get()] for key, combo in self._source_combos.items()},
                "replace_existing": bool(self._replace.get()),
                "ai_strategy": self._ai_strategy_labels[self._ai_strategy.get()]}

    def _needs_auto_ack(self):
        return bool(self._plan and self._plan["ai_strategy"] == "auto"
                    and LOCAL_PROXY_AI_SERVICE_IDS.intersection(self._plan["changed_services"]))

    def _show_view(self, view):
        if self._modal_destroyed or view not in self._pages:
            return
        if view == "preview":
            if self._choices() != self._last_choices:
                self._refresh()
            if self._plan is None:
                return
        if view != self._view:
            self._pages[self._view].pack_forget()
            self._view = view
            self._pages[view].pack(fill="x")
            self._body._parent_canvas.yview_moveto(0)
        if view == "preview":
            self._reviewed_choices = copy.deepcopy(self._choices())
        self._update_actions()

    def _update_actions(self):
        changes, kept, missing = self._counts
        preview = self._view == "preview"
        for tab, active in ((self._setup_tab, not preview), (self._preview_tab, preview)):
            _configure_changed(tab, fg_color=COLORS["primary" if active else "secondary"],
                               hover_color=COLORS["primary_hover" if active else "secondary_hover"])
        _configure_changed(self._preview_tab, state="normal" if self._plan is not None else "disabled")
        _configure_changed(self._back_button, text="返回设置" if preview else "取消")
        if self._plan is None:
            text, state, status = "先调整方案", "disabled", "无法生成预设；请查看上方原因。"
        elif self._accept_error:
            text, state, status = "请重新打开预设", "disabled", "未写入草稿；请查看上方提示。"
        else:
            state = "normal" if not preview or changes else "disabled"
            text = (f"写入 {changes} 项草稿" if changes else "先处理订阅问题" if missing else "无需修改") if preview else "核对分流"
            status = f"{changes} 项修改 · {kept} 项保留 · {missing} 项待处理"
            if not changes and not missing:
                if not self._replace.get():
                    status += "\n需重新分配？" + ("返回设置，" if preview else "") + "勾选重新规划。"
                else:
                    status += "\n已符合预设；关闭的目标仍保留。"
            else:
                status += "\n只生成草稿，保存并应用后生效。"
            if preview and self._needs_auto_ack() and not self._auto_ack.get():
                text, state = "先确认 AI 切换范围", "disabled"
                status = "AI 自动候选未锁定国家；请核对上方提示并确认，或返回设置使用固定节点。"
        _configure_changed(self._status, text=status,
                           text_color=COLORS["warning"] if missing or self._plan is None or self._accept_error else COLORS["muted"])
        _configure_changed(self._accept_button, text=text, state=state)

    def _refresh(self):
        if self._modal_destroyed:
            return
        choices = self._choices()
        if self._last_choices is not None and choices != self._last_choices:
            self._show_view("setup")
            self._reviewed_choices = None
            self._auto_ack.set(False)
        self._last_choices = copy.deepcopy(choices)
        self._accept_error = False
        self._error_detail.pack_forget()
        preset_id = choices["preset_id"]
        _configure_changed(self._description, text=ROUTE_PRESETS[preset_id]["description"])
        ai_hint = ("固定筛选后的推荐节点，失效不自动换节点；要故障切换，可在编辑器自选候选。\n"
                   "固定节点不保证供应商维持相同 IP / 国家。" if choices["ai_strategy"] == "fixed" else
                   "自动候选可随订阅刷新变化，可能跨国家；需在核对页明确确认。")
        if choices["ai_strategy"] == "fixed" and not choices["replace_existing"]:
            kept_auto = sum(bool(self._preferences.get("service_profile_bindings", {}).get(service))
                            and not self._preferences.get("service_node_bindings", {}).get(service)
                            and not self._preferences.get("service_node_pools", {}).get(service)
                            for service in LOCAL_PROXY_AI_SERVICE_IDS)
            if kept_auto:
                ai_hint += (f"\n仍保留 {kept_auto} 项已有 AI 自动切换线路；要固定，请勾选“重新规划已有分流”"
                            "或返回编辑器单独调整。")
        _configure_changed(self._ai_hint, text=ai_hint + "\n自动推荐排除香港及已知不合格节点；地区未知仍需实测。"
                           "\n仅影响本次将修改的 AI 目标；保留的已有线路不变。",
                           text_color=COLORS["muted"] if choices["ai_strategy"] == "fixed" else COLORS["warning"])
        for key, combo in self._source_combos.items():
            used = preset_id == "balanced" or (key == "residential" if preset_id == "ai_only" else key == "datacenter")
            _configure_changed(combo, state="readonly" if used else "disabled")
            card = self._source_rows[key][0]
            if used and not card.winfo_manager():
                card.pack(fill="x", pady=(0, 8))
            elif not used:
                card.pack_forget()
        # Restore canonical order when switching back from a single-source preset.
        siblings = self._sources_box.pack_slaves()
        ordered = [self._source_rows[key][0] for key in NETWORK_LABELS if self._source_rows[key][0] in siblings]
        if siblings != ordered:
            for card in reversed(ordered):
                if self._sources_box.pack_slaves()[0] is not card:
                    card.pack(before=self._sources_box.pack_slaves()[0])
        try:
            plan = plan_route_preset(self._preferences, self._catalog, **choices,
                                     protected_services=self._protected, strict_privacy=self._strict_privacy)
        except ValueError as exc:
            self._plan, self._reviewed_choices = None, None
            self._show_view("setup")
            self._error_detail.configure(text=safe_feedback_text(str(exc)))
            self._error_detail.pack(fill="x", padx=6, pady=(6, 10), before=self._pages[self._view])
            self._counts = (0, 0, 0)
            self._source_help.pack_forget()
            self._manage_sources_button.pack_forget()
            self._auto_ack_checkbox.pack_forget()
            self._update_actions()
            return
        self._plan = plan
        if self._needs_auto_ack():
            if not self._auto_ack_checkbox.winfo_manager():
                self._auto_ack_checkbox.pack(fill="x", padx=6, pady=(0, 10), after=self._preview_note)
        else:
            self._auto_ack_checkbox.pack_forget()
        labels = {item["id"]: " ".join(safe_feedback_text(str(item.get("name") or "未命名订阅")).split())
                  for item in self._catalog if isinstance(item, dict) and isinstance(item.get("id"), str)}
        needs_sources = False
        for key, source in plan["sources"].items():
            needs_sources |= not bool(source["id"])
            hint = self._source_rows[key][1]
            prefix = "已指定" if choices["sources"].get(key) else "推荐来源"
            ai_source = key == "residential" or preset_id == "datacenter"
            ai_count = source["ai_candidate_count"]
            eligibility = f"\nAI 自动候选：{ai_count} 个（已排除香港及已知不合格节点）" if ai_source else ""
            _configure_changed(hint, text=f"{prefix}：{labels.get(source['id'], '暂未分配')}\n{source['reason']}" + eligibility,
                               text_color=COLORS["warning"] if not source["id"] or ai_source and not ai_count else COLORS["muted"])
        if needs_sources:
            text = ("还没有订阅：请先返回代理页添加链接或导入节点。" if not self._catalog else
                    "先设置家宽 / 非家宽标记；无节点缓存时，返回代理页拉取订阅。")
            _configure_changed(self._source_help, text=text)
            if not self._source_help.winfo_manager():
                self._source_help.pack(fill="x", padx=6, pady=(10, 6))
            if self._catalog and self._on_manage_sources and not self._manage_sources_button.winfo_manager():
                self._manage_sources_button.pack(fill="x", padx=6, pady=(0, 8))
        else:
            self._source_help.pack_forget()
            self._manage_sources_button.pack_forget()
        rows = {row["id"]: row for row in proxy_routing.route_rows(plan["draft"])}
        statuses = {"changed": "将修改", "kept": "保留", "unavailable": "待处理", "unchanged": "无需修改"}
        kept = missing = 0
        for decision in plan["decisions"]:
            service, status = decision["service"], decision["status"]
            description = route_description(rows[service], plan["draft"], self._catalog)
            before = self._before[service]
            policy_warning = bool(decision.get("warning"))
            warning = status == "unavailable" or description["warning"] or policy_warning
            missing += int(bool(warning))
            kept += int(status in {"kept", "unchanged"})
            heading, detail = self._preview_rows[service]
            state = statuses[status] + (" · 需检查" if warning and status != "unavailable" else "")
            color = COLORS["warning" if warning else "accent" if status == "changed" else "muted"]
            _configure_changed(heading, text=decision["label"] + " · " + state, text_color=color)
            destination = description["profile"] + " → " + description["node"]
            if status == "changed":
                text = f"原：{before['profile']} → {before['node']}\n新：{destination}"
                if service in plan["draft"].get("service_node_bindings", {}):
                    text += "（固定节点）"
                if service in LOCAL_PROXY_AI_SERVICE_IDS:
                    text += "\n" + decision["reason"]
            elif status == "kept" and not warning:
                text = destination + ("\n未启用 · 不新增专属规则" if not rows[service]["enabled"] else "\n保留已有选择")
            else:
                text = destination + "\n" + (description["hint"] if description["warning"] and not policy_warning else decision["reason"])
            _configure_changed(detail, text=text, text_color=COLORS["warning"] if warning else COLORS["muted"])
        self._counts = (len(plan["changed_services"]), kept, missing)
        self._update_actions()

    def _back(self):
        if self._view == "preview":
            self._show_view("setup")
        else:
            self.destroy()

    def _primary_action(self):
        if self._view == "setup":
            self._show_view("preview")
        else:
            self._accept()

    def _manage_sources(self):
        if self._modal_destroyed or not self._on_manage_sources:
            return
        # Restore the editor's grab before opening another modal. Opening the
        # tag editor does not itself persist anything or accept this preview.
        callback = self._on_manage_sources
        self.destroy()
        callback()

    def _accept(self):
        if (self._modal_destroyed or self._view != "preview" or self._plan is None
                or not self._plan["changed_services"] or self._accept_error):
            return
        if self._reviewed_choices != self._choices():
            self._refresh()
            self._show_view("setup")
            return
        if self._needs_auto_ack() and not self._auto_ack.get():
            self._update_actions()
            return
        try:
            self._on_accept(**self._choices(), expected_plan=copy.deepcopy(self._plan))
        except Exception as exc:
            self._accept_error = True
            self._error_detail.configure(text=safe_feedback_text(str(exc)))
            self._error_detail.pack(fill="x", padx=6, pady=(6, 10), before=self._pages[self._view])
            self._body._parent_canvas.yview_moveto(0)
            self._update_actions()
            return
        self.destroy()
