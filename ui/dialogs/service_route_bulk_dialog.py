"""Batch route choices are pure drafts; no networking or persistence here."""
from __future__ import annotations

import copy

import customtkinter as ctk

from core import proxy_routing
from core.subscription_routing_policy import preferred_network_type, route_candidate_count, suggest_tagged_routes
from ui.dialogs.route_selection_dialogs import DraftChoiceDialog, RouteNodeDialog
from ui.feedback import safe_feedback_text
from ui.route_labels import DEFAULT_PROFILE, DIRECT_PROFILE, ORIGINAL_RULES, RESUME_ROUTE
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font
from ui.widgets.action_group import wrap_action_group

SET_ROUTE = "设置订阅与节点"
TAGGED = "按用途重新分配"
FOLLOW = DEFAULT_PROFILE
DIRECT = DIRECT_PROFILE
ENABLE = RESUME_ROUTE
DISABLE = ORIGINAL_RULES
OPERATIONS = (SET_ROUTE, DIRECT, FOLLOW, DISABLE, TAGGED, ENABLE)
CHOOSE_ROUTE = "请选择访问线路"


def _set_target_enabled(draft, service, row, enabled):
    if row["always"]:
        return
    if service.startswith("custom:"):
        for item in draft["custom_targets"]:
            if f"custom:{item['id']}" == service:
                item["enabled"] = enabled
    else:
        draft["builtin_sites"][service] = enabled


def apply_route_batch(preferences, catalog, services, operation, *, profile_id="", node_key="", node_keys=()):
    """Return an independent draft; reject invalid batches without partial edits."""
    draft = proxy_routing.normalize_routes(preferences)
    rows = {row["id"]: row for row in proxy_routing.route_rows(draft)}
    chosen = list(dict.fromkeys(services))
    if operation not in OPERATIONS or not chosen or any(key not in rows for key in chosen):
        raise ValueError("请选择有效的批量操作和目标；原草稿未修改")
    if operation == SET_ROUTE:
        profile = next((item for item in catalog if item["id"] == profile_id), None)
        available = {item["key"] for item in (profile or {}).get("nodes", ())}
        keys = list(node_keys)
        if profile is None or not available:
            raise ValueError("请选择有可用缓存的订阅")
        if (node_key and keys) or (node_key and node_key not in available):
            raise ValueError("固定节点选择无效，请重新选择")
        if (len(keys) > proxy_routing.MAX_SERVICE_NODE_POOL_SIZE or len(set(keys)) != len(keys)
                or any(key not in available for key in keys)):
            raise ValueError("自选候选列表已变化或超过数量限制，请重新选择")
        if not node_key and not keys and (profile.get("auto_route_usable") is False or not route_candidate_count(profile)):
            raise ValueError("该订阅没有可独立运行的首选节点，请指定节点或候选池")
    notices = []
    for service in chosen:
        row = rows[service]
        if operation in (ENABLE, DISABLE):
            if row["always"]:
                notices.append(f"{row['label']} 为默认启用目标，已保留其状态。")
                continue
            _set_target_enabled(draft, service, row, operation == ENABLE)
            continue
        # Recommendation failures must keep the target's original pinned route.
        candidate = copy.deepcopy(draft) if operation == TAGGED else draft
        candidate["service_profile_bindings"].pop(service, None)
        candidate["service_node_bindings"].pop(service, None)
        candidate.setdefault("service_node_pools", {}).pop(service, None)
        candidate["service_route_modes"].pop(service, None)
        if operation in (FOLLOW, DIRECT):
            candidate["service_route_modes"][service] = "direct" if operation == DIRECT else "default"
        elif operation == SET_ROUTE:
            candidate["service_profile_bindings"][service] = profile_id
            if node_key:
                candidate["service_node_bindings"][service] = node_key
            if node_keys:
                candidate["service_node_pools"][service] = list(node_keys)
        elif operation == TAGGED:
            if not preferred_network_type(service):
                notices.append(f"{row['label']} 无预设用途，已保留原选择。")
                continue
            original_sites = copy.deepcopy(candidate["builtin_sites"])
            if not row["always"]:
                candidate["builtin_sites"][service] = True
            candidate, reasons = suggest_tagged_routes(
                candidate, catalog, protected_services=set(rows) - {service},
            )
            candidate["builtin_sites"] = original_sites
            if service not in candidate["service_profile_bindings"]:
                notices.extend(reason for reason in reasons if "已保留" not in reason)
                continue
            draft = candidate
        # An explicit route choice has the same meaning in the single and bulk
        # editors: enable that rule. Failed recommendations above keep it intact.
        _set_target_enabled(draft, service, row, True)
    return proxy_routing.normalize_routes(draft), notices


class RouteBulkDialog(DraftChoiceDialog):
    def __init__(self, master, *, rows, catalog, on_apply, initial_services=(), scope_label=""):
        super().__init__(master)
        self.title("批量设置目标分流")
        self.geometry("760x700")
        self.minsize(560, 520)
        self.configure(fg_color=COLORS["app_bg"])
        self._rows = rows
        self._catalog = catalog
        self._on_apply = on_apply
        self._closed = False
        self._node_dialog = None
        self._node_key = ""
        self._node_keys = []
        self._profile_id = ""
        self._operation = ctk.StringVar(value=SET_ROUTE)
        self._vars = {}
        self._status_error = ""
        self._status_error_source = ""
        self._status_signature = None
        self._target_columns = 0
        self._layout_after_id = None
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=16)
        self._status = ctk.CTkLabel(footer, text="", anchor="w",
                                   justify="left", font=font(12), text_color=COLORS["muted"])
        self._status.pack(fill="x", pady=(0, 8))
        bind_wraplength(footer, self._status, padding=8)
        ctk.CTkButton(footer, text="取消", width=90, command=self.destroy, **button_style("secondary")).pack(side="left")
        self._apply_button = ctk.CTkButton(footer, text="确认选择，返回编辑", command=self._commit,
                                         state="disabled", **button_style("accent"))
        self._apply_button.pack(side="right")
        # Keep only the confirmation footer fixed. At high DPI, fixed settings
        # used to consume the entire height and hide every target checkbox.
        body = self._body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        heading = ctk.CTkLabel(body, text="批量设置线路", font=font(18, "bold"), anchor="w")
        heading.pack(fill="x", padx=6, pady=(4, 8))
        if scope_label:
            note = ctk.CTkLabel(body, text=f"位置：{safe_feedback_text(scope_label)}",
                               font=font(12), text_color=COLORS["muted"], anchor="w", justify="left")
            note.pack(fill="x", padx=6, pady=(0, 8))
            bind_wraplength(body, note, padding=12)
        settings = ctk.CTkFrame(body, fg_color=COLORS["surface"])
        settings.pack(fill="x", padx=6)
        settings.grid_columnconfigure(1, weight=1)
        self._profile_caption = ctk.CTkLabel(settings, text="访问线路", font=font(12))
        self._profile_caption.grid(row=0, column=0, padx=10, pady=10)
        self._profiles = {CHOOSE_ROUTE: ""}
        actions = [FOLLOW, DIRECT, DISABLE, ENABLE, TAGGED]
        reserved = {*actions, SET_ROUTE, CHOOSE_ROUTE}
        for item in catalog:
            label = safe_feedback_text(str(item["name"]))
            network = item.get("network_type")
            if network in {"residential", "datacenter"}:
                label += " · " + ("家宽" if network == "residential" else "非家宽")
            base, suffix = label, 2
            while label in self._profiles or label in reserved:
                label = f"{base} ({suffix})"
                suffix += 1
            self._profiles[label] = item["id"]
        self._profile = ctk.CTkComboBox(settings, values=[CHOOSE_ROUTE, *actions, *list(self._profiles)[1:]],
                                       state="readonly", command=self._select_route, **combo_style())
        self._profile.set(CHOOSE_ROUTE)
        self._profile.grid(row=0, column=1, sticky="ew", padx=10, pady=10)
        self._node_caption = ctk.CTkLabel(settings, text="节点与备用", font=font(12))
        self._node_button = ctk.CTkButton(settings, text="先选择订阅，再设置节点", command=self._open_nodes,
                                        state="disabled", **button_style("secondary"))
        self._selection_note = ctk.CTkLabel(body, text="", font=font(12), text_color=COLORS["muted"],
                                          anchor="w", justify="left")
        self._selection_note.pack(fill="x", padx=6, pady=(10, 0))
        bind_wraplength(body, self._selection_note, padding=12)
        toolbar = ctk.CTkFrame(body, fg_color="transparent")
        toolbar.pack(fill="x", padx=6, pady=10)
        for label, group in (("全选", "all"), ("AI 服务", "ai"), ("网站", "sites"), ("清空选择", "none")):
            ctk.CTkButton(toolbar, text=label, width=100, command=lambda group=group: self._select_group(group),
                          **button_style("secondary", compact=True)).pack(side="left", padx=(0, 6))
        wrap_action_group(toolbar, wide_columns=4)
        self._targets = ctk.CTkFrame(body, fg_color="transparent")
        self._targets.pack(fill="x", padx=6, pady=(0, 6))
        self._target_checks = {}
        self._target_labels = {}
        initial = set(initial_services)
        for row in rows:
            var = ctk.BooleanVar(value=row["id"] in initial)
            self._vars[row["id"]] = var
            check = ctk.CTkCheckBox(self._targets, text="", variable=var, command=self._changed,
                                   width=22, checkbox_width=18, checkbox_height=18)
            label = ctk.CTkLabel(self._targets, text=safe_feedback_text(row["label"]),
                                 font=font(12), anchor="w", justify="left", width=1, height=24)
            label.bind("<Button-1>", lambda _event, button=check: button.toggle())
            bind_wraplength(label, label, padding=4, min_width=1)
            self._target_checks[row["id"]] = check
            self._target_labels[row["id"]] = label
        self._targets.bind("<Configure>", self._schedule_target_layout, add="+")
        self._layout_targets()
        self._initial_selection = bool(initial & self._vars.keys())
        self._changed()
        center_window(self, master)
        self.grab_set()

    def _select_group(self, group):
        for row in self._rows:
            key = row["id"]
            self._vars[key].set(group == "all" or group == "ai" and key in {"openai", "claude", "google_ai"}
                                or group == "sites" and not row["always"] and not key.startswith("custom:"))
        self._changed()

    def _changed(self):
        count = sum(var.get() for var in self._vars.values())
        ready = self._operation.get() != SET_ROUTE or bool(self._profile_id)
        self._apply_button.configure(state="normal" if count and ready else "disabled", text=f"确认 {count} 项，返回编辑")
        prefix = "已带入筛选 · " if self._initial_selection else ""
        self._selection_note.configure(text=f"{prefix}勾选要修改的目标 · {count} / {len(self._vars)}")
        self._update_status(count=count, ready=ready)

    def _schedule_target_layout(self, event=None):
        # CTkFrame.bind delegates to its canvas, so real Configure events use
        # that widget as their source rather than the public frame object.
        sources = (self._targets, self._targets._canvas)
        if self._closed or event is not None and event.widget not in sources:
            return
        if self._layout_after_id is None:
            self._layout_after_id = self.after_idle(self._layout_targets)

    def _layout_targets(self):
        self._layout_after_id = None
        if self._closed:
            return
        width = self._targets.winfo_width() / self._targets._get_widget_scaling()
        columns = 2 if width >= 600 else 1
        if columns == self._target_columns:
            return
        self._target_columns = columns
        for column in range(4):
            self._targets.grid_columnconfigure(column, weight=1 if column % 2 and column < columns * 2 else 0,
                                               uniform="target-label" if column % 2 and column < columns * 2 else "")
        for index, service in enumerate(self._vars):
            row, column = divmod(index, columns)
            self._target_checks[service].grid(row=row, column=column * 2, sticky="nw", padx=(4, 4), pady=5)
            self._target_labels[service].grid(row=row, column=column * 2 + 1, sticky="ew", padx=(0, 12), pady=5)

    def _update_status(self, *, count=None, ready=None):
        count = sum(var.get() for var in self._vars.values()) if count is None else count
        if ready is None:
            ready = self._operation.get() != SET_ROUTE or bool(self._profile_id)
        if self._status_error:
            text = self._status_error
        elif not count:
            text = "请勾选目标并选择访问线路。" if not ready else "请勾选要修改的目标。"
        elif not ready:
            text = f"已选 {count} 项，请选择访问线路。"
        else:
            text = f"已选 {count} 项，确认后返回编辑。"
        detail = {
            DIRECT: "直连使用设备网络，不自动回退代理；不能与严格隐私同时启用。",
            DISABLE: "暂停网站专属规则，保留原线路，不等于直连；AI 服务保持启用。",
            ENABLE: "恢复之前的线路与节点。",
            TAGGED: "按用途分配并启用；没有合适订阅时保留原设置。",
        }.get(self._operation.get(), "")
        text = "\n".join(part for part in (text + " 保存并应用后生效。", detail) if part)
        signature = (text, COLORS["warning"] if self._status_error else COLORS["muted"])
        if signature != self._status_signature:
            self._status.configure(text=signature[0], text_color=signature[1])
            self._status_signature = signature

    def _show_error(self, text, *, source="validation"):
        self._status_error = safe_feedback_text(text)
        self._status_error_source = source
        self._update_status()

    def _clear_node_choice_error(self):
        if self._status_error_source == "node_choice":
            self._status_error = ""
            self._status_error_source = ""
            self._update_status()

    def _select_route(self, label):
        if label in self._profiles:
            self._select_profile(label)
        elif label in OPERATIONS and label != SET_ROUTE:
            self._operation_changed(label)

    def _operation_changed(self, operation):
        self._operation.set(operation)
        enabled = operation == SET_ROUTE and bool(self._profile_id)
        label = next((label for label, key in self._profiles.items() if key == self._profile_id), CHOOSE_ROUTE)
        self._profile.set(label if operation == SET_ROUTE else operation)
        for widget, col in ((self._node_caption, 0), (self._node_button, 1)):
            if enabled:
                widget.grid(row=1, column=col, sticky="ew" if col else "", padx=10, pady=(0, 10))
            else:
                widget.grid_forget()
        self._node_button.configure(state="normal" if enabled else "disabled")
        self._status_error = ""
        self._status_error_source = ""
        self._changed()

    def _select_profile(self, label):
        selected = self._profiles.get(label, "")
        if selected != self._profile_id:
            self._profile_id = selected
            self._node_key, self._node_keys = "", []
        self._update_node_label()
        self._operation_changed(SET_ROUTE)

    def _update_node_label(self):
        if self._node_keys:
            text = f"自选 {len(self._node_keys)} 个候选 · 自动切换 ›"
        elif self._node_key:
            text = "固定节点 · 不自动切换 ›"
        else:
            text = "订阅首选 + 故障切换 ›" if self._profile_id else "先选择订阅，再设置节点"
        self._node_button.configure(text=text)

    def _open_nodes(self):
        profile = next((item for item in self._catalog if item["id"] == self._profile_id), None)
        if profile is None:
            return
        if self._node_dialog and self._node_dialog.winfo_exists():
            self._node_dialog.lift()
            return
        self._node_dialog = RouteNodeDialog(
            self, service_label="批量目标", profile_name=profile["name"], nodes=profile["nodes"],
            selected_key=self._node_key, selected_keys=self._node_keys,
            on_select=lambda key: self._choose_node(key, profile_id=profile["id"]),
            on_select_pool=lambda keys: self._choose_pool(keys, profile_id=profile["id"]),
            auto_route_usable=profile.get("auto_route_usable", True),
            auto_route_candidate_count=profile.get("auto_route_candidate_count"),
        )

    def _choice_is_current(self, profile_id, keys):
        if self._closed:
            return False
        profile = next((item for item in self._catalog if item["id"] == self._profile_id), None)
        available = {item["key"] for item in (profile or {}).get("nodes", [])}
        if (profile is None or profile_id is not None and profile_id != self._profile_id
                or any(key not in available for key in keys)):
            self._show_error("订阅或节点列表已变化，原批量选择已保留，请重新选择节点。", source="node_choice")
            return False
        return True

    def _choose_node(self, key, *, profile_id=None):
        if not self._choice_is_current(profile_id, [key] if key else []):
            return
        self._node_key, self._node_keys = key, []
        self._update_node_label()
        self._clear_node_choice_error()

    def _choose_pool(self, keys, *, profile_id=None):
        if (not keys or len(keys) > proxy_routing.MAX_SERVICE_NODE_POOL_SIZE or len(set(keys)) != len(keys)
                or not self._choice_is_current(profile_id, keys)):
            return
        self._node_key, self._node_keys = "", list(keys)
        self._update_node_label()
        self._clear_node_choice_error()

    def _commit(self):
        if self._closed:
            return
        chosen = [key for key, var in self._vars.items() if var.get()]
        if not chosen:
            return
        if self._operation.get() == SET_ROUTE and not self._profile_id:
            self._show_error("请选择访问线路；指定订阅时需有可用缓存。")
            return
        try:
            self._on_apply(chosen, self._operation.get(), profile_id=self._profile_id,
                           node_key=self._node_key, node_keys=self._node_keys)
        except Exception as exc:
            self._show_error(str(exc))
            return
        self.destroy()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._layout_after_id is not None:
            self.after_cancel(self._layout_after_id)
            self._layout_after_id = None
        if self._node_dialog and self._node_dialog.winfo_exists():
            self._node_dialog.destroy()
        super().destroy()
