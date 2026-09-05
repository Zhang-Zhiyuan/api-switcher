"""One draft editor for Windows and per-server SSH service routing."""
from __future__ import annotations

import copy
import queue
import threading
import uuid

import customtkinter as ctk

from core import proxy_routing
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font, input_style, textbox_style

DEFAULT_PROFILE = "跟随默认线路"
DEFAULT_NODE = "订阅首选 + 故障切换"
MISSING_PROFILE = "订阅已失效，请重新选择"
MISSING_NODE = "固定节点已失效，请重新选择"


class ServiceRoutesDialog(ctk.CTkToplevel):
    def __init__(self, master, *, scopes, load_preferences, apply_preferences,
                 on_saved=None, initial_service="", catalog_loader=None):
        super().__init__(master)
        self.title("目标分流 · 订阅与节点")
        self.geometry("1040x760")
        self.minsize(680, 520)
        self.configure(fg_color=COLORS["app_bg"])
        self._scopes = list(dict.fromkeys(scopes))
        self._scope = self._scopes[0]
        self._loader = load_preferences
        self._applier = apply_preferences
        self._on_saved = on_saved
        self._catalog_loader = catalog_loader or proxy_routing.load_route_catalog
        self._drafts = {}
        self._originals = {}
        self._catalog = []
        self._rows = {}
        self._queue = queue.Queue()
        self._poll_id = None
        self._busy = True
        self._closed = False
        self._narrow = False
        self._initial_service = initial_service
        self.protocol("WM_DELETE_WINDOW", self._close)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 8))
        ctk.CTkLabel(header, text="按访问目标选择线路", font=font(20, "bold"),
                     text_color=COLORS["text"]).pack(anchor="w")
        notice = ctk.CTkLabel(
            header, text="先选订阅，再选节点；所有修改在点击“保存并应用”后生效。不同服务器分别保存。",
            font=font(12), text_color=COLORS["muted"], anchor="w", justify="left",
        )
        notice.pack(fill="x", pady=(4, 10))
        bind_wraplength(header, notice, padding=8)
        toolbar = ctk.CTkFrame(header, fg_color="transparent")
        toolbar.pack(fill="x")
        ctk.CTkLabel(toolbar, text="应用位置", font=font(12), text_color=COLORS["text"]).pack(side="left", padx=(0, 8))
        self._scope_combo = ctk.CTkComboBox(
            toolbar, values=self._scopes, width=225, state="readonly",
            command=self._switch_scope, **combo_style(),
        )
        self._scope_combo.set(self._scope)
        self._scope_combo.configure(state="disabled")
        self._scope_combo.pack(side="left")
        self._copy_button = None
        if len(self._scopes) > 1:
            self._copy_button = ctk.CTkButton(
                toolbar, text="复制到其他已选服务器", state="disabled",
                command=self._copy_to_scopes, **button_style("secondary", compact=True),
            )
            self._copy_button.pack(side="left", padx=8)

        search_bar = ctk.CTkFrame(self, fg_color="transparent")
        search_bar.pack(fill="x", padx=20, pady=(0, 8))
        self._search = ctk.StringVar(value="")
        ctk.CTkLabel(search_bar, text="筛选目标", font=font(12), text_color=COLORS["text"]).pack(side="left", padx=(0, 8))
        search = ctk.CTkEntry(search_bar, textvariable=self._search,
                            placeholder_text="筛选目标，例如 Claude、YouTube 或第三方 API 域名", **input_style())
        search.pack(side="left", fill="x", expand=True)
        self._search.trace_add("write", lambda *_args: self._filter_rows())

        self._table = ctk.CTkScrollableFrame(self, fg_color=COLORS["surface"])
        self._table.pack(fill="both", expand=True, padx=20)
        self._table.bind("<Configure>", self._on_resize, add="+")
        self._loading = ctk.CTkLabel(self._table, text="正在读取订阅、节点和已保存线路…", text_color=COLORS["muted"])
        self._loading.pack(pady=30)

        add_row = ctk.CTkFrame(self, fg_color="transparent")
        add_row.pack(fill="x", padx=20, pady=(10, 0))
        self._custom_entry = ctk.CTkEntry(add_row, placeholder_text="新增自定义域名、网址或 IP / CIDR", **input_style())
        self._custom_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._custom_entry.bind("<Return>", lambda _event: self._add_custom())
        self._add_button = ctk.CTkButton(add_row, text="添加目标", width=90, state="disabled",
                                       command=self._add_custom, **button_style("secondary", compact=True))
        self._add_button.pack(side="right")
        note = ctk.CTkLabel(
            self, text="自动模式仅在所选订阅内切换。固定节点不自动换出口；节点失效时保留原运行配置并提示。\n"
                       "未勾选目标不新增专属规则，仍遵循默认代理范围。第三方 API 请按域名单独添加。",
            font=font(11), text_color=COLORS["muted"], justify="left", anchor="w",
        )
        note.pack(fill="x", padx=20, pady=(8, 4))
        bind_wraplength(self, note, padding=44)
        self._status = ctk.CTkLabel(self, text="正在加载…", font=font(12), anchor="w", justify="left")
        self._status.pack(fill="x", padx=20)
        bind_wraplength(self, self._status, padding=44)
        self._details = ctk.CTkTextbox(self, height=84, **textbox_style())
        actions = ctk.CTkFrame(self, fg_color="transparent")
        self._actions = actions
        actions.pack(fill="x", padx=20, pady=(8, 16))
        self._reset_button = ctk.CTkButton(actions, text="撤销当前修改", command=self._reset,
                                         state="disabled", **button_style("secondary", compact=True))
        self._reset_button.pack(side="left")
        self._save_button = ctk.CTkButton(actions, text="保存并应用", command=self._apply,
                                        state="disabled", **button_style("accent"))
        self._save_button.pack(side="right")
        self._close_button = ctk.CTkButton(actions, text="关闭", command=self._close,
                                         **button_style("secondary", compact=True))
        self._close_button.pack(side="right", padx=8)
        center_window(self, master)
        self.grab_set()
        self._start_load()

    def _start_load(self):
        def run():
            try:
                preferences = {scope: proxy_routing.route_snapshot(self._loader(scope)) for scope in self._scopes}
                self._queue.put(("loaded", (preferences, self._catalog_loader())))
            except Exception as exc:
                self._queue.put(("error", safe_feedback_text(str(exc))))
        threading.Thread(target=run, name="service-routes-load", daemon=True).start()
        self._poll_id = self.after(60, self._poll)

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        try:
            event, payload = self._queue.get_nowait()
        except queue.Empty:
            self._poll_id = self.after(60, self._poll)
            return
        if event == "loaded":
            self._originals, self._catalog = payload
            self._drafts = copy.deepcopy(self._originals)
            self._busy = False
            self._render()
            if self._initial_service in self._rows:
                self._search.set(self._rows[self._initial_service]["label"])
            self._set_editable(True)
            self._status.configure(text="选择线路后可继续编辑其他目标，最后一次应用。", text_color=COLORS["muted"])
        elif event == "applied":
            succeeded, errors = payload
            for scope, preferences, _message in succeeded:
                self._originals[scope] = preferences
            self._busy = False
            self._set_editable(True)
            lines = [f"{scope}: {message}" for scope, _prefs, message in succeeded]
            lines.extend(f"{scope}: {error}" for scope, error in errors)
            self._status.configure(text=f"已应用 {len(succeeded)} 个位置，未成功 {len(errors)} 个。"
                                       + ("未成功项保留草稿，可再次应用重试。" if errors else ""),
                                   text_color=COLORS["warning"] if errors else COLORS["success"])
            self._details.pack(fill="x", padx=20, pady=(4, 0), before=self._actions)
            self._details.configure(state="normal")
            self._details.delete("1.0", "end")
            self._details.insert("1.0", safe_feedback_text("\n".join(lines)))
            self._details.configure(state="disabled")
            if succeeded and self._on_saved:
                self._on_saved()
        else:
            self._busy = False
            self._status.configure(text=f"读取失败：{payload}。请关闭后重试。", text_color=COLORS["danger"])

    def _profile_values(self):
        mapping = {DEFAULT_PROFILE: ""}
        for profile in self._catalog:
            label = str(profile["name"])
            if not profile["nodes"]:
                label += " · 请先拉取"
            base, index = label, 2
            while label in mapping:
                label = f"{base} ({index})"
                index += 1
            mapping[label] = profile["id"]
        return mapping

    def _node_values(self, profile_id):
        profile = next((item for item in self._catalog if item["id"] == profile_id), {})
        mapping = {DEFAULT_NODE: ""}
        for item in profile.get("nodes", []):
            base = str(item["label"])
            label, index = base, 2
            while label in mapping:
                label = f"{base} ({index})"
                index += 1
            mapping[label] = item["key"]
        return mapping

    def _render(self):
        for child in self._table.winfo_children():
            child.destroy()
        self._rows = {}
        draft = self._drafts[self._scope]
        profiles = self._profile_values()
        for row in proxy_routing.route_rows(draft):
            service = row["id"]
            tile = ctk.CTkFrame(self._table, fg_color="transparent")
            enabled = ctk.BooleanVar(value=row["enabled"])
            check = ctk.CTkCheckBox(
                tile, text=row["label"], variable=enabled, font=font(12),
                checkbox_width=16, checkbox_height=16, width=190,
                text_color=COLORS["text"], text_color_disabled=COLORS["muted"],
                command=lambda key=service, var=enabled: self._toggle(key, var.get()),
                state="disabled" if row["always"] else "normal",
            )
            profile_caption = ctk.CTkLabel(tile, text="订阅线路", font=font(10), text_color=COLORS["muted"], anchor="w")
            profile_combo = ctk.CTkComboBox(tile, values=list(profiles), state="readonly", width=210,
                                          command=lambda label, key=service: self._select_profile(key, label), **combo_style())
            node_caption = ctk.CTkLabel(tile, text="节点", font=font(10), text_color=COLORS["muted"], anchor="w")
            node_combo = ctk.CTkComboBox(tile, values=[DEFAULT_NODE], state="readonly", width=230,
                                       command=lambda label, key=service: self._select_node(key, label), **combo_style())
            delete = None
            if service.startswith("custom:"):
                delete = ctk.CTkButton(tile, text="移除", width=52, command=lambda key=service: self._remove_custom(key),
                                      **button_style("secondary", compact=True))
            self._rows[service] = {
                "tile": tile, "enabled": enabled, "check": check, "always": row["always"],
                "label": row["label"], "profile": profile_combo, "node": node_combo,
                "profile_caption": profile_caption, "node_caption": node_caption, "delete": delete,
            }
            self._refresh_row(service)
        self._layout_rows()
        self._filter_rows()

    def _refresh_row(self, service):
        row = self._rows[service]
        draft = self._drafts[self._scope]
        profile_id = draft["service_profile_bindings"].get(service, "")
        profiles = self._profile_values()
        label = next((label for label, value in profiles.items() if value == profile_id), MISSING_PROFILE)
        row["profile"].configure(state="readonly", values=[*profiles, *([MISSING_PROFILE] if label == MISSING_PROFILE else [])])
        row["profile"].set(label)
        row["profile"].configure(state="disabled" if self._busy else "readonly")
        nodes = self._node_values(profile_id)
        key = draft["service_node_bindings"].get(service, "")
        node_label = next((label for label, value in nodes.items() if value == key), MISSING_NODE)
        row["nodes"] = nodes
        row["node"].configure(values=[*nodes, *([MISSING_NODE] if node_label == MISSING_NODE else [])],
                              state="readonly", text_color_disabled=COLORS["muted"])
        row["node"].set(node_label if profile_id else ("跟随自定义默认线路" if service.startswith("custom:") else DEFAULT_PROFILE))
        row["node"].configure(state="readonly" if profile_id and not self._busy else "disabled")

    def _layout_rows(self):
        for row in self._rows.values():
            tile = row["tile"]
            for widget in tile.winfo_children():
                widget.grid_forget()
            tile.grid_columnconfigure(1, weight=1)
            tile.grid_columnconfigure(2, weight=1)
            row["check"].grid(row=0, column=0, rowspan=1 if self._narrow else 2,
                              columnspan=3 if self._narrow else 1, sticky="w", padx=(0, 12), pady=6)
            offset = 1 if self._narrow else 0
            row["profile_caption"].grid(row=offset, column=1, sticky="w", padx=6)
            row["node_caption"].grid(row=offset, column=2, sticky="w", padx=6)
            row["profile"].grid(row=offset + 1, column=1, sticky="ew", padx=6)
            row["node"].grid(row=offset + 1, column=2, sticky="ew", padx=6)
            if row["delete"]:
                row["delete"].grid(row=offset + 1, column=3, padx=(6, 0))

    def _on_resize(self, event):
        narrow = event.width / self._table._get_widget_scaling() < 860
        if narrow != self._narrow:
            self._narrow = narrow
            self._layout_rows()

    def _filter_rows(self):
        query = self._search.get().strip().casefold()
        for service, row in self._rows.items():
            row["tile"].pack_forget()
            aliases = "gpt chatgpt" if service == "openai" else ""
            if not query or query in f"{service} {row['label']} {aliases}".casefold():
                row["tile"].pack(fill="x", pady=(2, 12), padx=8)

    def _select_profile(self, service, label):
        if self._busy or label not in self._profile_values():
            return
        draft = self._drafts[self._scope]
        profile_id = self._profile_values()[label]
        if draft["service_profile_bindings"].get(service, "") == profile_id:
            return
        if profile_id:
            draft["service_profile_bindings"][service] = profile_id
            if not self._rows[service]["always"]:
                self._rows[service]["enabled"].set(True)
                self._toggle(service, True)
        else:
            draft["service_profile_bindings"].pop(service, None)
        draft["service_node_bindings"].pop(service, None)
        self._refresh_row(service)
        self._changed()

    def _select_node(self, service, label):
        if self._busy or label not in self._rows[service]["nodes"]:
            return
        key = self._rows[service]["nodes"][label]
        bindings = self._drafts[self._scope]["service_node_bindings"]
        if key:
            bindings[service] = key
        else:
            bindings.pop(service, None)
        self._changed()

    def _toggle(self, service, enabled):
        if self._busy:
            return
        draft = self._drafts[self._scope]
        if service.startswith("custom:"):
            for item in draft["custom_targets"]:
                if f"custom:{item['id']}" == service:
                    item["enabled"] = enabled
        else:
            draft["builtin_sites"][service] = enabled
        self._changed()

    def _changed(self):
        count = sum(self._drafts[scope] != self._originals[scope] for scope in self._scopes)
        self._status.configure(text=f"{count} 个应用位置有未保存修改；保存后统一生效。" if count else "当前线路未修改。",
                               text_color=COLORS["warning"] if count else COLORS["muted"])

    def _switch_scope(self, scope):
        if not self._busy and scope in self._drafts:
            self._scope = scope
            self._render()
            self._changed()

    def _copy_to_scopes(self):
        if self._busy:
            return
        for scope in self._scopes:
            if scope != self._scope:
                self._drafts[scope] = copy.deepcopy(self._drafts[self._scope])
        self._changed()

    def _add_custom(self):
        if self._busy:
            return
        try:
            normalized = proxy_routing.local_proxy.normalize_proxy_target(self._custom_entry.get())
        except ValueError as exc:
            self._status.configure(text=str(exc), text_color=COLORS["danger"])
            return
        targets = self._drafts[self._scope]["custom_targets"]
        if any(item["kind"] == normalized["kind"] and item["value"] == normalized["value"] for item in targets):
            self._status.configure(text="该目标已经存在，可通过上方筛选定位。", text_color=COLORS["warning"])
            return
        targets.append({**normalized, "id": uuid.uuid4().hex, "enabled": True,
                        "created_at": proxy_routing.remote_proxy._now_iso()})
        self._custom_entry.delete(0, "end")
        self._search.set(normalized["value"])
        self._render()
        self._changed()

    def _remove_custom(self, service):
        if self._busy:
            return
        draft = self._drafts[self._scope]
        draft["custom_targets"] = [item for item in draft["custom_targets"] if f"custom:{item['id']}" != service]
        draft["service_profile_bindings"].pop(service, None)
        draft["service_node_bindings"].pop(service, None)
        self._render()
        self._changed()

    def _reset(self):
        if not self._busy and self._scope in self._originals:
            self._drafts[self._scope] = copy.deepcopy(self._originals[self._scope])
            self._render()
            self._changed()

    def _set_editable(self, enabled):
        state = "normal" if enabled else "disabled"
        for button in (self._copy_button, self._add_button, self._reset_button, self._save_button):
            if button:
                button.configure(state=state)
        self._scope_combo.configure(state="readonly" if enabled else "disabled")
        self._custom_entry.configure(state=state)
        for service, row in self._rows.items():
            row["check"].configure(state="disabled" if row["always"] else state)
            row["profile"].configure(state="readonly" if enabled else "disabled")
            if row["delete"]:
                row["delete"].configure(state=state)
            self._refresh_row(service)

    def _apply(self):
        if self._busy or not self._drafts:
            return
        pending = {scope: copy.deepcopy(value) for scope, value in self._drafts.items()
                   if value != self._originals[scope]}
        if not pending:
            # Allow explicitly reapplying refreshed subscription caches.
            pending[self._scope] = copy.deepcopy(self._drafts[self._scope])
        originals = copy.deepcopy(self._originals)
        self._busy = True
        self._set_editable(False)
        self._status.configure(text="正在校验并应用线路，请稍候…", text_color=COLORS["muted"])
        def run():
            succeeded, errors = [], []
            for scope, preferences in pending.items():
                try:
                    message = self._applier(scope, preferences, originals[scope])
                    succeeded.append((scope, preferences, message))
                except Exception as exc:
                    errors.append((scope, safe_feedback_text(str(exc))))
            self._queue.put(("applied", (succeeded, errors)))
        threading.Thread(target=run, name="service-routes-apply", daemon=True).start()
        self._poll_id = self.after(60, self._poll)

    def _close(self):
        if self._busy and self._drafts:
            self._status.configure(text="线路正在应用，请等待结果后关闭。", text_color=COLORS["warning"])
            return
        if any(value != self._originals.get(scope) for scope, value in self._drafts.items()):
            ConfirmDialog(self, title="放弃未保存的线路修改", message="修改尚未应用，关闭将放弃本次草稿。", on_confirm=self.destroy)
        else:
            self.destroy()

    def destroy(self):
        self._closed = True
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        super().destroy()
