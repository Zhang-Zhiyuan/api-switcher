"""Batch route choices are pure drafts; no networking or persistence here."""
from __future__ import annotations

import copy

import customtkinter as ctk

from core import proxy_routing
from core.subscription_routing_policy import preferred_network_type, route_candidate_count, suggest_tagged_routes
from ui.dialogs.route_selection_dialogs import DraftChoiceDialog, RouteNodeDialog
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font

SET_ROUTE = "设置订阅与节点"
TAGGED = "按用途重新分配"
FOLLOW = "跟随默认线路"
DIRECT = "直连（不经过代理）"
ENABLE = "启用目标"
DISABLE = "停用目标"
OPERATIONS = (SET_ROUTE, DIRECT, TAGGED, FOLLOW, ENABLE, DISABLE)


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
            if service.startswith("custom:"):
                for item in draft["custom_targets"]:
                    if f"custom:{item['id']}" == service:
                        item["enabled"] = operation == ENABLE
            else:
                draft["builtin_sites"][service] = operation == ENABLE
            continue
        # Recommendation failures must keep the target's original pinned route.
        candidate = copy.deepcopy(draft) if operation == TAGGED else draft
        candidate["service_profile_bindings"].pop(service, None)
        candidate["service_node_bindings"].pop(service, None)
        candidate.setdefault("service_node_pools", {}).pop(service, None)
        candidate["service_route_modes"].pop(service, None)
        if operation in (FOLLOW, DIRECT):
            candidate["service_route_modes"][service] = "direct" if operation == DIRECT else "default"
            if operation == DIRECT and not row["always"]:
                if service.startswith("custom:"):
                    for item in candidate["custom_targets"]:
                        if f"custom:{item['id']}" == service:
                            item["enabled"] = True
                else:
                    candidate["builtin_sites"][service] = True
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
    return proxy_routing.normalize_routes(draft), notices


class RouteBulkDialog(DraftChoiceDialog):
    def __init__(self, master, *, rows, catalog, on_apply):
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
        self._vars = {}
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda _event: self.destroy())
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=18, pady=16)
        self._status = ctk.CTkLabel(footer, text="先勾选目标；设置线路不会改变原有启用状态。", anchor="w",
                                   justify="left", font=font(12), text_color=COLORS["muted"])
        self._status.pack(fill="x", pady=(0, 8))
        bind_wraplength(footer, self._status, padding=8)
        ctk.CTkButton(footer, text="取消", width=90, command=self.destroy, **button_style("secondary")).pack(side="left")
        self._apply_button = ctk.CTkButton(footer, text="写入所选目标草稿", command=self._commit,
                                         state="disabled", **button_style("accent"))
        self._apply_button.pack(side="right")
        heading = ctk.CTkLabel(self, text="一次设置多个业务的访问线路", font=font(18, "bold"), anchor="w")
        heading.pack(fill="x", padx=18, pady=(16, 8))
        note = ctk.CTkLabel(self, text="只修改当前位置的勾选目标，其他位置不变。\n关闭后可在修改清单预览，统一“保存并应用”才生效。",
                            font=font(12), text_color=COLORS["muted"], anchor="w", justify="left")
        note.pack(fill="x", padx=18, pady=(0, 10))
        bind_wraplength(self, note, padding=40)
        settings = ctk.CTkFrame(self, fg_color=COLORS["surface"])
        settings.pack(fill="x", padx=18)
        settings.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(settings, text="批量操作", font=font(12)).grid(row=0, column=0, padx=10, pady=10)
        self._operation = ctk.CTkComboBox(settings, values=list(OPERATIONS), state="readonly",
                                         command=self._operation_changed, **combo_style())
        self._operation.set(SET_ROUTE)
        self._operation.grid(row=0, column=1, sticky="ew", padx=10, pady=10)
        ctk.CTkLabel(settings, text="订阅线路", font=font(12)).grid(row=1, column=0, padx=10, pady=6)
        self._profiles = {"请选择订阅": ""}
        for item in catalog:
            label = safe_feedback_text(str(item["name"]))
            network = item.get("network_type")
            if network in {"residential", "datacenter"}:
                label += " · " + ("家宽" if network == "residential" else "非家宽")
            base, suffix = label, 2
            while label in self._profiles:
                label = f"{base} ({suffix})"
                suffix += 1
            self._profiles[label] = item["id"]
        self._profile = ctk.CTkComboBox(settings, values=list(self._profiles), state="readonly",
                                       command=self._select_profile, **combo_style())
        self._profile.set("请选择订阅")
        self._profile.grid(row=1, column=1, sticky="ew", padx=10, pady=6)
        ctk.CTkLabel(settings, text="节点策略", font=font(12)).grid(row=2, column=0, padx=10, pady=10)
        self._node_button = ctk.CTkButton(settings, text="先选择订阅，再设置节点", command=self._open_nodes,
                                        state="disabled", **button_style("secondary"))
        self._node_button.grid(row=2, column=1, sticky="ew", padx=10, pady=10)
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", padx=18, pady=10)
        for label, group in (("全选", "all"), ("AI 服务", "ai"), ("网站", "sites"), ("清空选择", "none")):
            ctk.CTkButton(toolbar, text=label, width=100, command=lambda group=group: self._select_group(group),
                          **button_style("secondary", compact=True)).pack(side="left", padx=(0, 6))
        body = ctk.CTkScrollableFrame(self, fg_color=COLORS["surface"])
        body.pack(fill="both", expand=True, padx=18)
        for row in rows:
            var = ctk.BooleanVar(value=False)
            self._vars[row["id"]] = var
            text = safe_feedback_text(row["label"]) + (" · 已启用" if row["enabled"] else " · 未启用")
            ctk.CTkCheckBox(body, text=text, variable=var, command=self._changed,
                            font=font(12), checkbox_width=18, checkbox_height=18).pack(fill="x", padx=10, pady=8)
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
        self._apply_button.configure(state="normal" if count else "disabled", text=f"写入 {count} 个目标草稿")

    def _operation_changed(self, operation):
        enabled = operation == SET_ROUTE
        self._profile.configure(state="readonly" if enabled else "disabled")
        self._node_button.configure(state="normal" if enabled and self._profile_id else "disabled")
        self._status.configure(
            text=("直连会启用所选目标，使用目标设备自身网络，不自动回退代理；严格隐私模式下不可启用。"
                  if operation == DIRECT else "先勾选目标；设置线路不会改变原有启用状态。"),
            text_color=COLORS["muted"],
        )

    def _select_profile(self, label):
        selected = self._profiles.get(label, "")
        if selected != self._profile_id:
            self._profile_id = selected
            self._node_key, self._node_keys = "", []
        self._update_node_label()
        self._operation_changed(self._operation.get())

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
            self._status.configure(text="订阅或节点列表已变化，原批量选择已保留，请重新选择节点。",
                                   text_color=COLORS["warning"])
            return False
        return True

    def _choose_node(self, key, *, profile_id=None):
        if not self._choice_is_current(profile_id, [key] if key else []):
            return
        self._node_key, self._node_keys = key, []
        self._update_node_label()

    def _choose_pool(self, keys, *, profile_id=None):
        if (not keys or len(keys) > proxy_routing.MAX_SERVICE_NODE_POOL_SIZE or len(set(keys)) != len(keys)
                or not self._choice_is_current(profile_id, keys)):
            return
        self._node_key, self._node_keys = "", list(keys)
        self._update_node_label()

    def _commit(self):
        if self._closed:
            return
        chosen = [key for key, var in self._vars.items() if var.get()]
        if not chosen:
            return
        try:
            self._on_apply(chosen, self._operation.get(), profile_id=self._profile_id,
                           node_key=self._node_key, node_keys=self._node_keys)
        except Exception as exc:
            self._status.configure(text=safe_feedback_text(str(exc)), text_color=COLORS["warning"])
            return
        self.destroy()

    def destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._node_dialog and self._node_dialog.winfo_exists():
            self._node_dialog.destroy()
        super().destroy()
