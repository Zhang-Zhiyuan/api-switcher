"""One draft editor for Windows and per-server SSH service routing."""
from __future__ import annotations

import copy
import queue
import threading
import uuid

import customtkinter as ctk

from core import proxy_routing
from core.subscription_routing_policy import preferred_network_type, suggest_tagged_routes
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font, input_style, textbox_style
from ui.widgets.service_route_overview import route_changes, route_description

DEFAULT_PROFILE = "跟随默认线路"
DEFAULT_CUSTOM_PROFILE = "跟随自定义默认线路"
DEFAULT_NODE = "订阅首选 + 故障切换"
AUTO_PROFILE = "按用途重新分配"
MISSING_PROFILE = "订阅已失效，请重新选择"
MISSING_NODE = "固定节点已失效，请重新选择"


def _unused_label(base, mapping, next_suffix):
    """Keep collision-safe labels while visiting each suffix only once per base."""
    if base not in mapping:
        return base
    index = next_suffix.get(base, 2)
    label = f"{base} ({index})"
    while label in mapping:
        index += 1
        label = f"{base} ({index})"
    next_suffix[base] = index + 1
    return label


class NodeChoiceButton(ctk.CTkButton):
    """Keep the selected label separate from the compact button presentation."""

    def set(self, value):
        self._value = value
        short = value if len(value) <= 28 else value[:27] + "…"
        self.configure(text=short + "  ›")

    def get(self):
        return getattr(self, "_value", "")


class ServiceRoutesDialog(ctk.CTkToplevel):
    def __init__(self, master, *, scopes, load_preferences, apply_preferences,
                 on_saved=None, initial_service="", catalog_loader=None, on_tags_saved=None):
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
        self._on_tags_saved = on_tags_saved
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
        self._filter_after_id = None
        self._category = "全部"
        self._node_dialog = None
        self._scope_copy_dialog = None
        self._bulk_dialog = None
        self._tags_dialog = None
        self._manually_edited = {scope: set() for scope in self._scopes}
        self._auto_seeded = set()
        self._default_notices = {}
        self._preview_open = False
        self._custom_open = False
        self._changes = []
        self.protocol("WM_DELETE_WINDOW", self._close)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(16, 8))
        ctk.CTkLabel(header, text="按访问目标选择线路", font=font(20, "bold"),
                     text_color=COLORS["text"]).pack(anchor="w")
        notice = ctk.CTkLabel(
            header, text="用途默认：AI、X/Reddit → 家宽；视频、搜索、下载、通信 → 非家宽。\n"
                         "自动补齐未配置目标，手动选择优先；保存并应用后生效，编辑期间不改变现有线路。",
            font=font(12), text_color=COLORS["muted"], anchor="w", justify="left",
        )
        notice.pack(fill="x", pady=(4, 10))
        bind_wraplength(header, notice, padding=8)
        toolbar = ctk.CTkFrame(header, fg_color="transparent")
        self._scope_toolbar = toolbar
        toolbar.pack(fill="x")
        toolbar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(toolbar, text="应用位置", font=font(12), text_color=COLORS["text"]).grid(row=0, column=0, padx=(0, 8))
        self._scope_combo = ctk.CTkComboBox(
            toolbar, values=self._scopes, width=225, state="readonly",
            command=self._switch_scope, **combo_style(),
        )
        self._scope_combo.set(self._scope)
        self._scope_combo.configure(state="disabled")
        if len(self._scopes) > 1:
            self._scope_combo.grid(row=0, column=1, sticky="ew")
        else:
            ctk.CTkLabel(toolbar, text=self._scope, font=font(12), text_color=COLORS["text"],
                         anchor="w").grid(row=0, column=1, sticky="ew")
        self._copy_button = None
        if len(self._scopes) > 1:
            self._copy_button = ctk.CTkButton(
                toolbar, text="复制到指定服务器…", state="disabled",
                command=self._open_copy_dialog, **button_style("secondary", compact=True),
            )
            self._copy_button.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self._scope_inline = None
        toolbar.bind("<Configure>", self._layout_scope_toolbar, add="+")
        self._layout_scope_toolbar()

        filters = ctk.CTkFrame(self, fg_color="transparent")
        filters.pack(fill="x", padx=20, pady=(0, 8))
        self._categories = ctk.CTkSegmentedButton(
            filters, values=["全部", "AI 服务", "网站", "自定义", "待修复"], command=self._set_category,
            font=font(12), selected_color=COLORS["primary"], unselected_color=COLORS["secondary"],
        )
        self._categories.pack(fill="x")
        self._categories.set("全部")

        search_bar = ctk.CTkFrame(self, fg_color="transparent")
        search_bar.pack(fill="x", padx=20, pady=(0, 8))
        self._search = ctk.StringVar(value="")
        ctk.CTkLabel(search_bar, text="搜索目标 / 已选线路", font=font(12), text_color=COLORS["text"]).pack(side="left", padx=(0, 8))
        search = ctk.CTkEntry(search_bar, textvariable=self._search,
                            placeholder_text="目标 / 订阅 / 节点名称", **input_style())
        search.pack(side="left", fill="x", expand=True)
        self._clear_filter_button = ctk.CTkButton(search_bar, text="清除筛选", width=80, command=self._clear_filters,
                                                 **button_style("secondary", compact=True))
        self._clear_filter_button.pack(side="left", padx=(8, 0))
        self._search.trace_add("write", lambda *_args: self._schedule_filter())
        self._count_label = ctk.CTkLabel(self, text="", font=font(11), text_color=COLORS["muted"], anchor="w", height=20)
        self._count_label.pack(fill="x", padx=22, pady=(0, 4))
        bind_wraplength(self, self._count_label, padding=44)

        self._table = ctk.CTkScrollableFrame(self, fg_color=COLORS["surface"])
        self._table.pack(fill="both", expand=True, padx=20)
        self._table.bind("<Configure>", self._on_resize, add="+")
        self._loading = ctk.CTkLabel(self._table, text="正在读取订阅、节点和已保存线路…", text_color=COLORS["muted"])
        self._loading.pack(pady=30)

        # Reserve footer space before the scroll area: small windows must never
        # clip Save/Close below the table's requested height.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", before=header)
        tools_row = ctk.CTkFrame(footer, fg_color="transparent")
        tools_row.pack(fill="x", padx=20, pady=(6, 2))
        self._custom_toggle = ctk.CTkButton(tools_row, text="＋ 自定义目标", width=112, command=self._toggle_custom_form,
                                           **button_style("secondary", compact=True))
        self._custom_toggle.pack(side="left")
        self._tags_button = ctk.CTkButton(tools_row, text="订阅标记", width=88, state="disabled",
                                         command=self._open_subscription_tags, **button_style("secondary", compact=True))
        self._tags_button.pack(side="left", padx=(8, 0))
        self._tag_routes_button = ctk.CTkButton(tools_row, text="补齐默认分流", width=106, state="disabled",
                                               command=self._suggest_tagged_routes, **button_style("secondary", compact=True))
        self._tag_routes_button.pack(side="left", padx=(8, 0))
        self._bulk_button = ctk.CTkButton(tools_row, text="批量设置", width=88, state="disabled",
                                         command=self._open_bulk_dialog, **button_style("primary", compact=True))
        self._bulk_button.pack(side="left", padx=(8, 0))
        self._preview_toggle = ctk.CTkButton(tools_row, text="修改清单（0）", width=140, command=self._toggle_preview,
                                            **button_style("secondary", compact=True))
        self._preview_toggle.pack(side="right")
        add_row = ctk.CTkFrame(footer, fg_color="transparent")
        self._custom_form = add_row
        self._custom_entry = ctk.CTkEntry(add_row, placeholder_text="新增自定义域名、网址或 IP / CIDR", **input_style())
        self._custom_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._custom_entry.bind("<Return>", lambda _event: self._add_custom())
        self._add_button = ctk.CTkButton(add_row, text="添加目标", width=90, state="disabled",
                                       command=self._add_custom, **button_style("secondary", compact=True))
        self._add_button.pack(side="right")
        note = ctk.CTkLabel(
            footer, text="未启用的目标仍遵循默认代理范围，并不等于直连。第三方 API 请添加实际域名；自定义规则优先。",
            font=font(11), text_color=COLORS["muted"], justify="left", anchor="w", height=20,
        )
        note.pack(fill="x", padx=20, pady=(8, 4))
        bind_wraplength(self, note, padding=44)
        self._status = ctk.CTkLabel(footer, text="正在加载…", font=font(12), anchor="w", justify="left", height=22)
        self._status.pack(fill="x", padx=20)
        bind_wraplength(self, self._status, padding=44)
        self._details = ctk.CTkTextbox(footer, height=84, **textbox_style())
        self._preview = ctk.CTkTextbox(footer, height=110, **textbox_style())
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        self._actions = actions
        actions.pack(fill="x", padx=20, pady=(8, 16))
        self._reset_button = ctk.CTkButton(actions, text="撤销当前修改", command=self._reset, width=100,
                                         state="disabled", **button_style("secondary", compact=True))
        self._reload_button = ctk.CTkButton(actions, text="重读订阅缓存", command=self._reload_catalog, width=100,
                                          state="disabled", **button_style("secondary", compact=True))
        self._save_button = ctk.CTkButton(actions, text="保存并应用", command=self._apply, width=112,
                                        state="disabled", **button_style("accent"))
        self._close_button = ctk.CTkButton(actions, text="关闭", command=self._close, width=70,
                                         **button_style("secondary", compact=True))
        self._action_columns = None
        actions.bind("<Configure>", self._layout_actions, add="+")
        self._layout_actions()
        center_window(self, master)
        self.grab_set()
        self._start_load()

    def _toggle_custom_form(self):
        self._custom_open = not self._custom_open
        if self._custom_open:
            self._custom_form.pack(fill="x", padx=20, pady=(6, 0), before=self._status)
            self._custom_entry.focus_set()
        else:
            self._custom_form.pack_forget()
        self._custom_toggle.configure(text="收起自定义输入" if self._custom_open else "＋ 自定义目标")

    def _toggle_preview(self):
        self._preview_open = not self._preview_open
        self._update_preview()

    def _update_preview(self):
        count = len(self._changes)
        self._preview_toggle.configure(text=f"{'收起' if self._preview_open else '查看'}修改清单（{count}）")
        if not self._preview_open:
            self._preview.pack_forget()
            return
        lines = []
        for item in self._changes:
            lines.extend([f"[{item['scope']}] {item['label']}", f"  原：{item['before']}",
                          f"  新：{'移除目标及其独立绑定' if item['removed'] else item['after']}", ""])
        text = "\n".join(lines) if lines else f"没有未保存修改。再次应用只处理当前位置：{self._scope}。"
        notices = self._default_notices.get(self._scope, [])
        if notices:
            text += "\n\n按用途分流说明：\n" + "\n".join(notices)
        self._preview.configure(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", safe_feedback_text(text))
        self._preview.configure(state="disabled")
        self._preview.pack(fill="x", padx=20, pady=(4, 0), before=self._actions)

    def _layout_actions(self, event=None):
        width = (event.width if event else self._actions.winfo_width()) / self._actions._get_widget_scaling()
        columns = 4 if width >= 620 else 2
        if columns == self._action_columns:
            return
        self._action_columns = columns
        for col in range(4):
            self._actions.grid_columnconfigure(col, weight=1 if col < columns else 0, uniform="route-actions" if col < columns else "")
        for index, button in enumerate((self._reset_button, self._reload_button, self._close_button, self._save_button)):
            button.grid(row=index // columns, column=index % columns, sticky="ew",
                        padx=(0, 8) if index % columns < columns - 1 else 0, pady=(4, 0))

    def _layout_scope_toolbar(self, event=None):
        if self._copy_button is None:
            return
        width = (event.width if event else self._scope_toolbar.winfo_width()) / self._scope_toolbar._get_widget_scaling()
        inline = width >= 600
        if inline == self._scope_inline:
            return
        self._scope_inline = inline
        self._copy_button.grid_forget()
        self._copy_button.grid(row=0 if inline else 1, column=2 if inline else 1, sticky="w",
                               padx=(8, 0) if inline else 0, pady=0 if inline else (6, 0))

    def _reload_catalog(self):
        if self._busy:
            return
        self._busy = True
        self._set_editable(False)
        self._status.configure(text="正在重读本地订阅与节点缓存，未保存修改会保留…", text_color=COLORS["muted"])
        def run():
            try:
                self._queue.put(("catalog", self._catalog_loader()))
            except Exception as exc:
                self._queue.put(("catalog_error", safe_feedback_text(str(exc))))
        self._start_worker(run, "service-routes-catalog")

    def _start_worker(self, run, name):
        """A thread allocation failure must not lock an unsaved draft forever."""
        try:
            threading.Thread(target=run, name=name, daemon=True).start()
        except Exception as exc:
            self._busy = False
            if self._drafts:
                self._set_editable(True)
                self._changed()
            detail = safe_feedback_text(str(exc)) or type(exc).__name__
            recovery = "草稿和现有线路未改变，可重试或关闭。" if self._drafts else "请关闭窗口后重新打开。"
            self._status.configure(text=f"后台任务未启动：{detail}。{recovery}", text_color=COLORS["danger"])
            return
        self._poll_id = self.after(60, self._poll)

    def _open_subscription_tags(self):
        if self._busy or self._closed:
            return
        if self._tags_dialog and self._tags_dialog.winfo_exists():
            self._tags_dialog.lift()
            return
        from ui.dialogs.subscription_tags_dialog import SubscriptionTagsDialog

        self._tags_dialog = SubscriptionTagsDialog(self, catalog=self._catalog, on_saved=self._subscription_tags_saved)

    def _subscription_tags_saved(self):
        if self._closed:
            return
        self._reload_catalog()
        if self._on_tags_saved:
            self._on_tags_saved()

    def _suggest_tagged_routes(self):
        if self._busy or self._closed or self._scope not in self._drafts:
            return
        before = self._drafts[self._scope]
        protected = self._manually_edited[self._scope] | {key for key in self._rows if self._row_changed(key)}
        draft, notices = suggest_tagged_routes(before, self._catalog, protected_services=protected)
        self._default_notices[self._scope] = notices
        added = sum(key not in before["service_profile_bindings"] for key in draft["service_profile_bindings"])
        self._drafts[self._scope] = draft
        self._preview_open = True
        self._clear_filters()
        self._render()
        self._changed()
        self._details.pack_forget()
        self._status.configure(
            text=f"已补齐 {added} 项草稿：AI、X/Reddit 优先家宽，视频/搜索/下载/通信优先非家宽。"
                 "保留已有绑定、已关闭目标和手动修改；保存并应用后生效。",
            text_color=COLORS["accent"] if added else COLORS["warning"],
        )

    def _seed_tagged_defaults(self, *, reload=False):
        """Populate only the visited scope's draft; never persist or apply here."""
        if self._scope in self._auto_seeded and not reload:
            return
        self._auto_seeded.add(self._scope)
        if not any(item.get("network_type") in {"residential", "datacenter"} for item in self._catalog):
            self._default_notices.pop(self._scope, None)
            return
        before = self._drafts[self._scope]
        draft, notices = suggest_tagged_routes(
            before, self._catalog, protected_services=self._manually_edited[self._scope],
        )
        self._drafts[self._scope] = draft
        self._default_notices[self._scope] = notices

    def _use_tagged_default(self, service):
        """Explicitly restore a single target's recommendation, not other rows."""
        if self._busy or not preferred_network_type(service):
            return
        draft = copy.deepcopy(self._drafts[self._scope])
        draft["service_profile_bindings"].pop(service, None)
        draft["service_node_bindings"].pop(service, None)
        draft.get("service_node_pools", {}).pop(service, None)
        draft.setdefault("service_route_modes", {}).pop(service, None)
        if not self._rows[service]["always"]:
            draft["builtin_sites"][service] = True
        draft, notices = suggest_tagged_routes(
            draft, self._catalog, protected_services=proxy_routing.service_ids(draft) - {service},
        )
        # A missing/ambiguous recommendation must not discard a working manual
        # route or fixed node. Explain the next step and leave the draft intact.
        if not draft["service_profile_bindings"].get(service):
            self._refresh_row(service)
            self._status.configure(text="无法按用途分配，请标记并拉取订阅，或手动选择该目标的订阅。原选择已保留。",
                                   text_color=COLORS["warning"])
            self._default_notices[self._scope] = notices
            self._preview_open = True
            self._update_preview()
            return
        self._drafts[self._scope] = draft
        self._manually_edited[self._scope].add(service)
        self._default_notices[self._scope] = notices
        self._render()
        self._changed(service)

    def _start_load(self):
        def run():
            try:
                preferences = {scope: proxy_routing.route_snapshot(self._loader(scope)) for scope in self._scopes}
                self._queue.put(("loaded", (preferences, self._catalog_loader())))
            except Exception as exc:
                self._queue.put(("error", safe_feedback_text(str(exc))))
        self._start_worker(run, "service-routes-load")

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
            self._seed_tagged_defaults()
            self._render()
            if self._initial_service in self._rows:
                self._search.set(self._rows[self._initial_service]["label"])
            self._set_editable(True)
            self._changed()
        elif event in ("catalog", "catalog_error"):
            self._busy = False
            if event == "catalog":
                if payload != self._catalog:
                    self._auto_seeded.clear()
                self._catalog = payload
                self._seed_tagged_defaults(reload=True)
                self._render()
            self._set_editable(True)
            self._changed()
            if event == "catalog_error":
                self._status.configure(text=f"缓存读取失败：{payload}。已保留草稿和原有列表。", text_color=COLORS["danger"])
        elif event == "applied":
            succeeded, errors = payload
            for scope, preferences, _message in succeeded:
                self._originals[scope] = preferences
                self._manually_edited[scope].clear()
                self._default_notices.pop(scope, None)
            self._busy = False
            self._set_editable(True)
            self._preview_open = False
            self._changed()
            lines = [f"{scope}: {message}" for scope, _prefs, message in succeeded]
            lines.extend(f"{scope}: {error}" for scope, error in errors)
            self._status.configure(text=f"已处理 {len(succeeded)} 个位置，未成功 {len(errors)} 个；实际结果见下方。"
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

    def _ensure_mapping_cache(self):
        # Catalogs are replaced atomically after background reads. Retain the
        # original object, not only id(), to avoid object-ID reuse on reload.
        if (getattr(self, "_mapping_catalog", None) is not self._catalog
                or getattr(self, "_mapping_catalog_size", -1) != len(self._catalog)):
            self._mapping_catalog = self._catalog
            self._mapping_catalog_size = len(self._catalog)
            self._mapping_profiles = {item["id"]: item for item in self._catalog}
            self._profile_mapping_cache = {}
            self._profile_reverse_cache = {}
            self._node_mapping_cache = {}
            self._node_reverse_cache = {}

    def _profile_values(self, service=""):
        self._ensure_mapping_cache()
        default = DEFAULT_CUSTOM_PROFILE if service.startswith("custom:") else DEFAULT_PROFILE
        # Metadata-only edits can update a catalog entry without replacing its
        # node list. This small signature avoids repeating redaction per row.
        signature = tuple((item["id"], item["name"], item.get("network_type"), bool(item.get("nodes")))
                          for item in self._catalog)
        cached = self._profile_mapping_cache.get(default)
        if cached and cached[0] == signature:
            return cached[1]
        mapping, suffixes = {default: ""}, {}
        for profile in self._catalog:
            label = " ".join(safe_feedback_text(str(profile["name"])).split())
            if profile.get("network_type") in {"residential", "datacenter"}:
                label += " · " + ("家宽" if profile["network_type"] == "residential" else "非家宽")
            if not profile["nodes"]:
                label += " · 请先拉取"
            if label == AUTO_PROFILE:
                label += "（订阅）"
            label = _unused_label(label, mapping, suffixes)
            mapping[label] = profile["id"]
        self._profile_mapping_cache[default] = (signature, mapping)
        reverse = {}
        for label, value in mapping.items():
            reverse.setdefault(value, label)
        self._profile_reverse_cache[default] = reverse
        return mapping

    def _node_values(self, profile_id):
        self._ensure_mapping_cache()
        profile = self._mapping_profiles.get(profile_id, {})
        source = profile.get("nodes", ())
        cached = self._node_mapping_cache.get(profile_id)
        if cached and cached[0] is source and cached[1] == len(source):
            return cached[2]
        mapping, suffixes = {DEFAULT_NODE: ""}, {}
        for item in source:
            base = " ".join(safe_feedback_text(str(item["label"])).split())
            label = _unused_label(base, mapping, suffixes)
            mapping[label] = item["key"]
        self._node_mapping_cache[profile_id] = (source, len(source), mapping)
        reverse = {}
        for label, key in mapping.items():
            reverse.setdefault(key, label)
        self._node_reverse_cache[profile_id] = reverse
        return mapping

    def _render(self):
        if self._loading.winfo_exists():
            self._loading.destroy()
        draft = self._drafts[self._scope]
        infos = proxy_routing.route_rows(draft)
        keys = {info["id"] for info in infos}
        for key in self._rows.keys() - keys:
            self._rows.pop(key)["tile"].destroy()
        if not hasattr(self, "_empty"):
            self._empty = ctk.CTkLabel(
                self._table, text="没有匹配的目标。试试清除筛选，或在下方添加自定义目标。",
                text_color=COLORS["muted"], font=font(12), anchor="w", justify="left",
            )
            bind_wraplength(self._table, self._empty, padding=30)
        for info in infos:
            service = info["id"]
            if service in self._rows:
                row = self._rows[service]
                row["info"], row["label"] = info, info["label"]
                row["enabled"].set(info["enabled"])
                row["name"].configure(text=safe_feedback_text(info["label"]))
                self._refresh_row(service)
                continue
            profiles = self._profile_values(service)
            tile = ctk.CTkFrame(self._table, fg_color=COLORS["surface_alt"], corner_radius=8)
            target = ctk.CTkFrame(tile, fg_color="transparent")
            target.grid_columnconfigure(1, weight=1)
            enabled = ctk.BooleanVar(value=info["enabled"])
            check = ctk.CTkCheckBox(
                target, text="", variable=enabled, width=22,
                checkbox_width=16, checkbox_height=16,
                command=lambda key=service, var=enabled: self._toggle(key, var.get()),
                state="disabled" if info["always"] else "normal",
            )
            if not info["always"]:
                check.grid(row=0, column=0, sticky="nw", padx=(0, 4))
            name = ctk.CTkLabel(
                target, text=safe_feedback_text(info["label"]), font=font(12, "bold"),
                text_color=COLORS["text"], anchor="w", justify="left", width=1, height=22,
            )
            name.grid(row=0, column=1, sticky="ew")
            bind_wraplength(target, name, padding=30, min_width=130)
            state_label = ctk.CTkLabel(target, text="", font=font(10), text_color=COLORS["muted"], anchor="w", height=16)
            state_label.grid(row=1, column=1, sticky="w")
            profile_caption = ctk.CTkLabel(tile, text="订阅线路", font=font(10), text_color=COLORS["muted"], anchor="w", height=16)
            profile_combo = ctk.CTkComboBox(
                tile, values=list(profiles), state="readonly", width=160,
                command=lambda label, key=service: self._select_profile(key, label), **combo_style(),
            )
            node_caption = ctk.CTkLabel(tile, text="节点策略 · 点击搜索 / 设置", font=font(10), text_color=COLORS["muted"], anchor="w", height=16)
            node_combo = NodeChoiceButton(
                tile, text="", width=170, anchor="w", command=lambda key=service: self._open_node_picker(key),
                **{**button_style("secondary"), "font": font(12)},
            )
            detail = ctk.CTkLabel(tile, text="", font=font(11), text_color=COLORS["muted_soft"],
                                  anchor="w", justify="left", width=1, height=18)
            bind_wraplength(detail, detail, padding=4, min_width=220)
            delete = None
            if service.startswith("custom:"):
                delete = ctk.CTkButton(tile, text="移除", width=52, command=lambda key=service: self._remove_custom(key),
                                      **button_style("secondary", compact=True))
            self._rows[service] = {
                "tile": tile, "enabled": enabled, "check": check, "always": info["always"],
                "label": info["label"], "info": info, "profile": profile_combo, "node": node_combo,
                "target": target, "state_label": state_label, "detail": detail, "name": name,
                "profile_caption": profile_caption, "node_caption": node_caption, "delete": delete,
            }
            self._refresh_row(service)
        self._rows = {info["id"]: self._rows[info["id"]] for info in infos}
        self._layout_rows()
        self._filter_rows()

    def _refresh_row(self, service):
        row = self._rows[service]
        draft = self._drafts[self._scope]
        profile_id = draft["service_profile_bindings"].get(service, "")
        profiles = self._profile_values(service)
        default = DEFAULT_CUSTOM_PROFILE if service.startswith("custom:") else DEFAULT_PROFILE
        label = self._profile_reverse_cache[default].get(profile_id, MISSING_PROFILE)
        row["profile"].configure(state="readonly", values=[
            *profiles, *([AUTO_PROFILE] if preferred_network_type(service) else []),
            *([MISSING_PROFILE] if label == MISSING_PROFILE else []),
        ])
        row["profile"].set(label)
        row["profile"].configure(state="disabled" if self._busy else "readonly")
        nodes = self._node_values(profile_id)
        key = draft["service_node_bindings"].get(service, "")
        pool = draft.get("service_node_pools", {}).get(service, [])
        node_label = self._node_reverse_cache[profile_id].get(key, MISSING_NODE)
        row["nodes"] = nodes
        row["node"].set(node_label if profile_id else (DEFAULT_CUSTOM_PROFILE if service.startswith("custom:") else DEFAULT_PROFILE))
        if pool:
            row["node"].set(f"自选 {len(pool)} 个候选 · 自动切换")
        row["node"].configure(state="normal" if profile_id and not self._busy else "disabled", text_color_disabled=COLORS["muted"])
        row["info"]["enabled"] = bool(row["enabled"].get())
        description = route_description(row["info"], draft, self._catalog)
        row["description"] = description
        dirty = self._row_changed(service)
        state = "继承基准" if service == "custom" else ("默认启用" if row["always"] else ("已启用" if row["enabled"].get() else "未启用"))
        preferred = preferred_network_type(service)
        if preferred:
            state += " · 默认" + ("家宽" if preferred == "residential" else "非家宽")
        row["state_label"].configure(text=state + (" · 未保存" if dirty else ""),
                                      text_color=COLORS["accent"] if dirty else COLORS["muted"])
        # Full names stay visible here when the dropdown entry is too narrow.
        detail = description["hint"]
        if profile_id or description["inherited"]:
            detail = f'{description["profile"]} → {description["node"]} · {detail}'
        elif description["source_hint"] and not description["warning"]:
            detail = description["source_hint"]
        row["detail"].configure(text=detail, text_color=COLORS["warning"] if description["warning"] else COLORS["muted_soft"])
        row["tile"].configure(border_width=1 if dirty or description["warning"] else 0,
                               border_color=COLORS["warning"] if description["warning"] else COLORS["accent"] if dirty else COLORS["border_soft"])

    def _row_changed(self, service):
        def value(preferences):
            enabled = True
            if service.startswith("custom:"):
                enabled = next((item for item in preferences["custom_targets"] if f"custom:{item['id']}" == service), None)
            elif not self._rows[service]["always"]:
                enabled = bool(preferences["builtin_sites"].get(service))
            return (enabled, preferences["service_profile_bindings"].get(service, ""),
                    preferences["service_node_bindings"].get(service, ""),
                    tuple(preferences.get("service_node_pools", {}).get(service, [])),
                    preferences.get("service_route_modes", {}).get(service, ""))
        return value(self._drafts[self._scope]) != value(self._originals[self._scope])

    def _layout_rows(self):
        for row in self._rows.values():
            tile = row["tile"]
            for widget in tile.winfo_children():
                widget.grid_forget()
            for col in range(4):
                tile.grid_columnconfigure(col, weight=0, minsize=0, uniform="")
            if self._narrow:
                tile.grid_columnconfigure((0, 1), weight=1, uniform="route-editor")
                row["target"].grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 2))
                offset, profile_col, node_col = 1, 0, 1
            else:
                tile.grid_columnconfigure(0, minsize=round(190 * self._table._get_widget_scaling()))
                tile.grid_columnconfigure((1, 2), weight=1, uniform="route-editor")
                row["target"].grid(row=0, column=0, rowspan=2, sticky="ew", padx=12, pady=8)
                offset, profile_col, node_col = 0, 1, 2
            row["profile_caption"].grid(row=offset, column=profile_col, sticky="w", padx=8, pady=(4, 0))
            row["node_caption"].grid(row=offset, column=node_col, sticky="w", padx=8, pady=(4, 0))
            row["profile"].grid(row=offset + 1, column=profile_col, sticky="ew", padx=8)
            row["node"].grid(row=offset + 1, column=node_col, sticky="ew", padx=8)
            row["detail"].grid(row=offset + 2, column=profile_col, columnspan=2, sticky="ew", padx=8, pady=(4, 8))
            if row["delete"]:
                row["delete"].grid(row=0 if self._narrow else 1, column=2 if self._narrow else 3, padx=(0, 8), pady=4)

    def _on_resize(self, event):
        narrow = event.width / self._table._get_widget_scaling() < 860
        if narrow != self._narrow:
            self._narrow = narrow
            self._layout_rows()

    def _schedule_filter(self):
        if self._filter_after_id:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(120, self._filter_rows)

    def _set_category(self, category):
        self._category = category
        self._categories.set(category)
        self._filter_rows()

    def _clear_filters(self):
        self._category = "全部"
        self._categories.set("全部")
        self._search.set("")
        self._filter_rows()

    def _filter_rows(self):
        if self._filter_after_id:
            self.after_cancel(self._filter_after_id)
            self._filter_after_id = None
        if not hasattr(self, "_empty"):
            return
        query = self._search.get().strip().casefold()
        self._empty.pack_forget()
        visible = 0
        for service, row in self._rows.items():
            row["tile"].pack_forget()
            aliases = "gpt chatgpt" if service == "openai" else ""
            description = row["description"]
            text = f"{service} {row['label']} {aliases} {description['profile']} {description['node']}".casefold()
            category = ("自定义" if service == "custom" or service.startswith("custom:")
                        else "AI 服务" if row["always"] else "网站")
            show = (not query or query in text) and (
                self._category == "全部" or self._category == category
                or self._category == "待修复" and description["warning"])
            if show:
                row["tile"].pack(fill="x", pady=(0, 6), padx=2)
                visible += 1
        if not visible:
            self._empty.pack(fill="x", padx=12, pady=24)
        self._count_label.configure(text=f"显示 {visible} / {len(self._rows)} 项 · 勾选网站才新增专属规则；AI 服务默认启用")

    def _select_profile(self, service, label):
        if label == AUTO_PROFILE:
            self._use_tagged_default(service)
            return
        profiles = self._profile_values(service)
        if self._busy or label not in profiles:
            return
        self._manually_edited[self._scope].add(service)
        draft = self._drafts[self._scope]
        profile_id = profiles[label]
        modes = draft.setdefault("service_route_modes", {})
        if profile_id:
            modes.pop(service, None)
        elif preferred_network_type(service):
            modes[service] = "default"
        if draft["service_profile_bindings"].get(service, "") == profile_id:
            self._changed(service)
            return
        had_fixed_node = bool(draft["service_node_bindings"].get(service) or draft.get("service_node_pools", {}).get(service))
        if profile_id:
            draft["service_profile_bindings"][service] = profile_id
        else:
            draft["service_profile_bindings"].pop(service, None)
        draft["service_node_bindings"].pop(service, None)
        draft.get("service_node_pools", {}).pop(service, None)
        if profile_id and not self._rows[service]["always"]:
            self._toggle(service, True)
        else:
            self._changed(service)
        if had_fixed_node:
            self._status.configure(text="订阅已更改，原固定节点或候选池已从草稿解除。请确认新的节点策略后再保存。",
                                   text_color=COLORS["warning"])

    def _open_node_picker(self, service):
        if self._busy or self._closed:
            return
        if self._node_dialog and self._node_dialog.winfo_exists():
            self._node_dialog.lift()
            return
        scope = self._scope
        profile_id = self._drafts[scope]["service_profile_bindings"].get(service, "")
        profile = next((item for item in self._catalog if item["id"] == profile_id), None)
        if profile is None:
            self._status.configure(text="请先为该目标选择一个有效订阅；默认线路不单独指定节点。", text_color=COLORS["warning"])
            return
        from ui.dialogs.route_selection_dialogs import RouteNodeDialog

        # Use the same collision-safe labels as the row; apply by stable key.
        nodes = [{"key": key, "label": label} for label, key in self._node_values(profile_id).items() if key]
        self._node_dialog = RouteNodeDialog(
            self, service_label=self._rows[service]["label"], profile_name=profile["name"], nodes=nodes,
            selected_key=self._drafts[scope]["service_node_bindings"].get(service, ""),
            selected_keys=self._drafts[scope].get("service_node_pools", {}).get(service, []),
            on_select=lambda key: self._accept_node_choice(scope, service, profile_id, key),
            on_select_pool=lambda keys: self._accept_node_pool(scope, service, profile_id, keys),
            auto_route_usable=profile.get("auto_route_usable", True),
            auto_route_candidate_count=profile.get("auto_route_candidate_count"),
        )

    def _accept_node_choice(self, scope, service, profile_id, key):
        if self._busy or self._closed or scope != self._scope or service not in self._rows:
            return
        current_profile = self._drafts[scope]["service_profile_bindings"].get(service, "")
        nodes = self._node_values(current_profile)
        if current_profile != profile_id or key not in nodes.values():
            self._status.configure(text="订阅或节点列表已经变化，请重新选择；原草稿未被覆盖。", text_color=COLORS["warning"])
            return
        label = next(label for label, value in nodes.items() if value == key)
        self._select_node(service, label)

    def _accept_node_pool(self, scope, service, profile_id, keys):
        if self._busy or self._closed or scope != self._scope or service not in self._rows:
            return
        draft = self._drafts[scope]
        available = set(self._node_values(profile_id).values()) - {""}
        if (draft["service_profile_bindings"].get(service) != profile_id
                or not keys or len(keys) > proxy_routing.MAX_SERVICE_NODE_POOL_SIZE
                or len(set(keys)) != len(keys) or any(key not in available for key in keys)):
            self._status.configure(text="订阅或候选节点已经变化，请重新选择；原草稿未被覆盖。", text_color=COLORS["warning"])
            return
        draft["service_node_bindings"].pop(service, None)
        draft.setdefault("service_node_pools", {})[service] = list(keys)
        self._manually_edited[scope].add(service)
        self._changed(service)

    def _select_node(self, service, label):
        if self._busy or label not in self._rows[service]["nodes"]:
            return
        self._manually_edited[self._scope].add(service)
        key = self._rows[service]["nodes"][label]
        bindings = self._drafts[self._scope]["service_node_bindings"]
        self._drafts[self._scope].get("service_node_pools", {}).pop(service, None)
        if key:
            bindings[service] = key
        else:
            bindings.pop(service, None)
        self._changed(service)

    def _toggle(self, service, enabled):
        if self._busy:
            return
        if self._rows[service]["always"]:
            return
        self._manually_edited[self._scope].add(service)
        self._rows[service]["enabled"].set(bool(enabled))
        draft = self._drafts[self._scope]
        if service.startswith("custom:"):
            for item in draft["custom_targets"]:
                if f"custom:{item['id']}" == service:
                    item["enabled"] = enabled
        else:
            draft["builtin_sites"][service] = enabled
        self._changed(service)

    def _changed(self, service=""):
        count = sum(self._drafts[scope] != self._originals[scope] for scope in self._scopes)
        self._changes = route_changes(self._originals, self._drafts, self._catalog)
        keys = ([service] + [key for key in self._rows if key.startswith("custom:") and service == "custom"]) if service else self._rows
        for key in keys:
            self._refresh_row(key)
        self._filter_rows()
        self._reset_button.configure(state="normal" if not self._busy and self._drafts[self._scope] != self._originals[self._scope] else "disabled")
        self._save_button.configure(text=f"保存并应用（{count}）" if count > 1 else "保存并应用")
        if count:
            self._details.pack_forget()
        self._update_preview()
        self._status.configure(text=f"待保存：{count} 个位置、{len(self._changes)} 项目标变化（含继承影响）。可展开修改清单核对。" if count else "无未保存修改；可重新应用当前位置。连通状态需单独检查。",
                               text_color=COLORS["accent"] if count else COLORS["muted"])
        if self._default_notices.get(self._scope):
            missing = sum("未分配：" in notice for notice in self._default_notices[self._scope])
            if missing:
                self._status.configure(text=self._status.cget("text") + " 部分用途未能自动分配，展开修改清单查看原因。",
                                       text_color=COLORS["warning"])

    def _switch_scope(self, scope):
        if not self._busy and scope in self._drafts:
            self._scope = scope
            self._scope_combo.set(scope)
            self._seed_tagged_defaults()
            self._render()
            self._changed()

    def _open_copy_dialog(self):
        if self._busy or len(self._scopes) < 2:
            return
        if self._scope_copy_dialog and self._scope_copy_dialog.winfo_exists():
            self._scope_copy_dialog.lift()
            return
        from ui.dialogs.route_selection_dialogs import RouteScopeCopyDialog

        self._scope_copy_dialog = RouteScopeCopyDialog(
            self, source=self._scope, scopes=[scope for scope in self._scopes if scope != self._scope],
            dirty_scopes={scope for scope in self._scopes if self._drafts[scope] != self._originals[scope]},
            on_copy=self._copy_to_scopes,
        )

    def _open_bulk_dialog(self):
        if self._busy or self._closed:
            return
        if self._bulk_dialog and self._bulk_dialog.winfo_exists():
            self._bulk_dialog.lift()
            return
        from ui.dialogs.service_route_bulk_dialog import RouteBulkDialog

        scope = self._scope
        self._bulk_dialog = RouteBulkDialog(
            self, rows=proxy_routing.route_rows(self._drafts[scope]), catalog=self._catalog,
            on_apply=lambda services, operation, **choices: self._accept_bulk_edit(scope, services, operation, **choices),
        )

    def _accept_bulk_edit(self, scope, services, operation, **choices):
        if self._busy or self._closed or scope != self._scope:
            raise ValueError("当前位置已变化，请重新打开批量设置")
        from ui.dialogs.service_route_bulk_dialog import apply_route_batch

        draft, notices = apply_route_batch(self._drafts[scope], self._catalog, services, operation, **choices)
        self._drafts[scope] = draft
        self._manually_edited[scope].update(services)
        self._default_notices[scope] = notices
        self._preview_open = True
        self._render()
        self._changed()

    def _copy_to_scopes(self, targets):
        if self._busy:
            return
        for scope in dict.fromkeys(targets):
            if scope in self._drafts and scope != self._scope:
                self._drafts[scope] = copy.deepcopy(self._drafts[self._scope])
                self._manually_edited[scope] = set(self._manually_edited[self._scope])
                self._auto_seeded.add(scope)
                self._default_notices[scope] = list(self._default_notices.get(self._scope, []))
        self._preview_open = True
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
            self._set_category("自定义")
            self._categories.set("自定义")
            self._search.set(normalized["value"])
            self._filter_rows()
            self._status.configure(text="该目标已经存在，已为你定位。", text_color=COLORS["warning"])
            return
        targets.append({**normalized, "id": uuid.uuid4().hex, "enabled": True,
                        "created_at": proxy_routing.remote_proxy._now_iso()})
        self._custom_entry.delete(0, "end")
        self._category = "自定义"
        self._categories.set("自定义")
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
        draft.get("service_node_pools", {}).pop(service, None)
        draft.get("service_route_modes", {}).pop(service, None)
        self._render()
        self._changed()

    def _reset(self):
        if not self._busy and self._scope in self._originals:
            self._drafts[self._scope] = copy.deepcopy(self._originals[self._scope])
            self._manually_edited[self._scope].clear()
            self._default_notices.pop(self._scope, None)
            self._render()
            self._changed()

    def _set_editable(self, enabled):
        state = "normal" if enabled else "disabled"
        for button in (self._copy_button, self._add_button, self._reset_button, self._save_button, self._reload_button,
                       self._tags_button, self._tag_routes_button, self._bulk_button):
            if button:
                button.configure(state=state)
        self._scope_combo.configure(state="readonly" if enabled and len(self._scopes) > 1 else "disabled")
        self._custom_entry.configure(state=state)
        self._custom_toggle.configure(state=state)
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
        self._start_worker(run, "service-routes-apply")

    def _close(self):
        if self._busy and self._drafts:
            self._status.configure(text="线路正在处理，请等待结果后关闭。", text_color=COLORS["warning"])
            return
        if any(value != self._originals.get(scope) for scope, value in self._drafts.items()):
            ConfirmDialog(self, title="放弃未保存的线路修改", message="修改尚未应用，关闭将放弃本次草稿。", on_confirm=self.destroy)
        else:
            self.destroy()

    def destroy(self):
        self._closed = True
        for dialog in (self._node_dialog, self._scope_copy_dialog, self._tags_dialog, self._bulk_dialog):
            if dialog and dialog.winfo_exists():
                dialog.destroy()
        if self._filter_after_id:
            try:
                self.after_cancel(self._filter_after_id)
            except Exception:
                pass
            self._filter_after_id = None
        if self._poll_id:
            try:
                self.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
        super().destroy()
