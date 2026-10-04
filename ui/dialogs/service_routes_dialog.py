"""One draft editor for Windows and per-server SSH service routing."""
from __future__ import annotations

import copy
import queue
import threading
import uuid

import customtkinter as ctk

from core import proxy_routing
from core.proxy_route_preview import route_preflight
from core.subscription_routing_policy import (
    cleanup_legacy_social_routes, legacy_social_route_candidates,
    preferred_network_type, suggest_tagged_routes,
)
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.feedback import safe_feedback_text
from ui.route_labels import DEFAULT_CUSTOM_PROFILE, DEFAULT_PROFILE, DIRECT_PROFILE, ORIGINAL_RULES, RESUME_ROUTE
from ui.theme import COLORS, bind_wraplength, button_style, center_window, combo_style, font, input_style, textbox_style
from ui.widgets.service_route_overview import route_changes, route_description

DEFAULT_NODE = "订阅首选 + 故障切换"
AUTO_PROFILE = "按用途重新分配"
MISSING_PROFILE = "订阅已失效，请重新选择"
MISSING_NODE = "固定节点已失效，请重新选择"


def _configure_changed(widget, **options):
    """CTk redraws even unchanged options; avoid that work for reused rows."""
    changed = {key: value for key, value in options.items() if widget.cget(key) != value}
    if changed:
        widget.configure(**changed)


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
        if getattr(self, "_value", None) == value:
            return
        self._value = value
        short = value if len(value) <= 28 else value[:27] + "…"
        self.configure(text=short + "  ›")

    def get(self):
        return getattr(self, "_value", "")


class ServiceRoutesDialog(ctk.CTkToplevel):
    def __init__(self, master, *, scopes, load_preferences, apply_preferences,
                 on_saved=None, initial_service="", catalog_loader=None, on_tags_saved=None,
                 recover_preferences=None, initial_preset=False):
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
        self._recoverer = recover_preferences
        self._contexts = {}
        self._preflight_cache = {}
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
        self._preset_requested = bool(initial_preset)
        self._filter_after_id = None
        self._category = "全部"
        self._node_dialog = None
        self._scope_copy_dialog = None
        self._bulk_dialog = None
        self._preset_dialog = None
        self._tags_dialog = None
        self._scope_confirm_dialog = None
        self._manually_edited = {scope: set() for scope in self._scopes}
        self._default_notices = {}
        self._preview_open = False
        self._results_open = False
        self._more_open = False
        self._content_view = None
        self._custom_open = False
        self._changes = []
        self.protocol("WM_DELETE_WINDOW", self._close)

        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=20)
        self._table = ctk.CTkScrollableFrame(self._content, fg_color=COLORS["surface"])
        self._table.pack(fill="both", expand=True)
        self._table.bind("<Configure>", self._on_resize, add="+")
        # Real parentage matters: CTk dispatches wheel events by walking master,
        # not by inspecting pack(in_=...). Every scrollable control belongs here.
        header = ctk.CTkFrame(self._table, fg_color="transparent")
        self._header = header
        header.pack(fill="x", padx=20, pady=(12, 8))
        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x")
        ctk.CTkLabel(title_row, text="目标分流", font=font(20, "bold"),
                     text_color=COLORS["text"]).pack(side="left")
        self._preset_button = ctk.CTkButton(title_row, text="智能分流方案", width=130, state="disabled",
                                           command=self._open_preset_dialog, **button_style("primary", compact=True))
        self._preset_button.pack(side="right")
        notice = ctk.CTkLabel(
            header, text="每个目标只选一种访问方式，再保存应用。选订阅后可设置固定节点或备用。",
            font=font(12), text_color=COLORS["muted"], anchor="w", justify="left",
        )
        notice.pack(fill="x", pady=(2, 6))
        self._intro_notice = notice
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
        self._recovery_button = None
        if recover_preferences:
            self._recovery_button = ctk.CTkButton(
                header, text="从远端恢复本地分流记录（不改变远端）", state="disabled",
                command=self._recover_routes, **button_style("secondary", compact=True),
            )
            self._recovery_button.pack(anchor="w", pady=(6, 0))

        filters = ctk.CTkFrame(self._table, fg_color="transparent")
        self._filters = filters
        filters.pack(fill="x", padx=20, pady=(0, 4))
        self._categories = ctk.CTkSegmentedButton(
            filters, values=["全部", "AI 服务", "网站", "自定义", "待修复"], command=self._set_category,
            font=font(12), selected_color=COLORS["primary"], unselected_color=COLORS["secondary"],
        )
        self._categories.pack(fill="x")
        self._categories.set("全部")

        search_bar = ctk.CTkFrame(filters, fg_color="transparent")
        search_bar.pack(fill="x", pady=(6, 2))
        self._search = ctk.StringVar(value="")
        ctk.CTkLabel(search_bar, text="搜索", font=font(12), text_color=COLORS["text"]).pack(side="left", padx=(0, 8))
        search = ctk.CTkEntry(search_bar, textvariable=self._search,
                            placeholder_text="目标 / 订阅 / 节点名称", **input_style())
        search.pack(side="left", fill="x", expand=True)
        self._clear_filter_button = ctk.CTkButton(search_bar, text="清除筛选", width=80, command=self._clear_filters,
                                                 **button_style("secondary", compact=True))
        self._clear_filter_button.pack(side="left", padx=(8, 0))
        self._search.trace_add("write", lambda *_args: self._schedule_filter())
        self._count_label = ctk.CTkLabel(filters, text="", font=font(11), text_color=COLORS["muted"], anchor="w", height=20)
        self._count_label.pack(fill="x", pady=(0, 2))
        bind_wraplength(filters, self._count_label, padding=4)

        # Filters scroll with the routes instead of consuming a fixed-height
        # band. Large text must not push the editable rows out of the window.
        filters.pack_forget()
        filters.pack(in_=self._table, fill="x", padx=8, pady=(0, 6), after=self._header)
        self._loading = ctk.CTkLabel(self._table, text="正在读取订阅、节点和已保存线路…", text_color=COLORS["muted"])
        self._loading.pack(pady=30)

        # Reserve footer space before the scroll area: small windows must never
        # clip Save/Close below the table's requested height.
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", before=self._content)
        tools_row = ctk.CTkFrame(self._table, fg_color="transparent")
        self._tools_row = tools_row
        self._tools_columns = None
        tools_row.pack(fill="x", padx=8, pady=(0, 8), after=self._filters)
        self._more_tools = ctk.CTkFrame(self._table, fg_color=COLORS["surface_alt"], corner_radius=6)
        self._more_toggle = ctk.CTkButton(tools_row, text="更多 / 说明 ▾", width=80, command=self._toggle_more,
                                        **button_style("secondary", compact=True))
        self._custom_toggle = ctk.CTkButton(tools_row, text="＋ 自定义", width=80, command=self._toggle_custom_form,
                                           **button_style("secondary", compact=True))
        self._tags_button = ctk.CTkButton(self._more_tools, text="标记家宽 / 非家宽", width=100, state="disabled",
                                         command=self._open_subscription_tags, **button_style("secondary", compact=True))
        self._tag_routes_button = ctk.CTkButton(self._more_tools, text="补齐默认分流", width=100, state="disabled",
                                               command=self._suggest_tagged_routes, **button_style("secondary", compact=True))
        self._legacy_cleanup_button = ctk.CTkButton(
            self._more_tools, text="整理旧版分流", width=100, state="disabled",
            command=self._cleanup_legacy_routes, **button_style("secondary", compact=True),
        )
        self._bulk_button = ctk.CTkButton(tools_row, text="批量设置", width=88, state="disabled",
                                         command=self._open_bulk_dialog, **button_style("secondary", compact=True))
        self._preview_toggle = ctk.CTkButton(tools_row, text="修改清单", width=80, state="disabled", command=self._toggle_preview,
                                            **button_style("secondary", compact=True))
        tools_row.bind("<Configure>", self._layout_tools, add="+")
        add_row = ctk.CTkFrame(self._table, fg_color="transparent")
        self._custom_form = add_row
        self._custom_entry = ctk.CTkEntry(add_row, placeholder_text="新增自定义域名、网址或 IP / CIDR", **input_style())
        self._custom_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self._custom_entry.bind("<Return>", lambda _event: self._add_custom())
        self._add_button = ctk.CTkButton(add_row, text="添加目标", width=90, state="disabled",
                                       command=self._add_custom, **button_style("secondary", compact=True))
        self._add_button.pack(side="right")
        note = ctk.CTkLabel(
            self._more_tools, text="用途默认：AI → 家宽，其他内置网站 → 非家宽；手动选择优先。\n"
                                   "补齐和整理只生成草稿。直连需自身网络可达，不能与严格隐私同时启用。",
            font=font(11), text_color=COLORS["muted"], justify="left", anchor="w", height=20,
        )
        note.grid(row=3, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 8))
        self._footer_note = note
        bind_wraplength(self._more_tools, note, padding=20)
        self._status = ctk.CTkLabel(footer, text="正在加载…", font=font(12), anchor="w", justify="left", height=22)
        self._status.pack(fill="x", padx=20)
        bind_wraplength(self, self._status, padding=44)
        self._review_heading = ctk.CTkLabel(self._content, text="", font=font(14, "bold"),
                                          text_color=COLORS["text"], anchor="w")
        self._details = ctk.CTkTextbox(self._content, height=120, **textbox_style())
        self._preview = ctk.CTkTextbox(self._content, height=120, **textbox_style())
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        self._actions = actions
        actions.pack(fill="x", padx=20, pady=(4, 8))
        self._reset_button = ctk.CTkButton(actions, text="撤销修改", command=self._reset, width=100,
                                         state="disabled", **button_style("secondary", compact=True))
        self._reload_button = ctk.CTkButton(self._more_tools, text="重读订阅缓存", command=self._reload_catalog, width=100,
                                          state="disabled", **button_style("secondary", compact=True))
        self._reapply_button = ctk.CTkButton(self._more_tools, text="重新应用当前线路", command=self._reapply_current,
                                            state="disabled", **button_style("secondary", compact=True))
        self._save_button = ctk.CTkButton(actions, text="保存并应用", command=self._apply, width=112,
                                        state="disabled", **button_style("accent"))
        self._close_button = ctk.CTkButton(actions, text="关闭", command=self._close, width=70,
                                         **button_style("secondary", compact=True))
        self._back_button = ctk.CTkButton(actions, text="返回编辑", command=self._toggle_preview, width=90,
                                         **button_style("secondary", compact=True))
        self._action_columns = None
        for col in range(2):
            self._more_tools.grid_columnconfigure(col, weight=1, uniform="route-more")
        for index, button in enumerate((self._tags_button, self._tag_routes_button, self._reload_button, self._legacy_cleanup_button)):
            button.grid(row=index // 2, column=index % 2, sticky="ew", padx=8, pady=(6, 0))
        self._reapply_button.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 0))
        self._layout_tools()
        actions.bind("<Configure>", self._layout_actions, add="+")
        self._layout_actions()
        # Keep only actions/status fixed. The setup header belongs to the same
        # scrolling surface as filters and routes, including at high DPI.
        header.pack_forget()
        header.pack(in_=self._table, fill="x", padx=8, pady=(12, 8), before=self._filters)
        center_window(self, master)
        self.grab_set()
        self._start_load()

    def _toggle_custom_form(self):
        self._preview_open = self._results_open = False
        self._update_preview()
        self._custom_open = not self._custom_open
        if self._custom_open:
            if self._more_open:
                self._toggle_more()
            self._custom_form.pack(fill="x", padx=8, pady=(0, 8), after=self._tools_row)
            self._custom_entry.focus_set()
            self._reveal_table_widget(self._custom_form)
        else:
            self._custom_form.pack_forget()
        self._custom_toggle.configure(text="收起输入" if self._custom_open else "＋ 自定义")

    def _toggle_preview(self):
        if self._busy or not self._drafts:
            return
        if self._results_open:
            self._results_open = False
            self._preview_open = False
        else:
            self._preview_open = not self._preview_open
        self._update_preview()

    def _toggle_more(self):
        if not self._more_open and self._content_view != "edit":
            self._preview_open = self._results_open = False
            self._update_preview()
        self._more_open = not self._more_open
        if self._more_open:
            if self._custom_open:
                self._custom_open = False
                self._custom_form.pack_forget()
                self._custom_toggle.configure(text="＋ 自定义")
            self._more_tools.pack(fill="x", padx=8, pady=(4, 8), after=self._tools_row)
        else:
            self._more_tools.pack_forget()
        self._more_toggle.configure(text="收起说明 ▴" if self._more_open else "更多 / 说明 ▾")
        self._layout_rows()
        if self._more_open:
            self._reveal_table_widget(self._more_tools)

    def _reveal_table_widget(self, widget):
        self._table.update_idletasks()
        viewport = self._table._parent_canvas
        bounds = viewport.bbox("all")
        if bounds and bounds[3] > bounds[1]:
            top = widget.winfo_rooty() - viewport.winfo_rooty() + viewport.canvasy(0)
            viewport.yview_moveto(max(0, top - bounds[1] - 4) / (bounds[3] - bounds[1]))

    def _sync_content_view(self):
        """Review/results use the content area, never shrink the editor footer."""
        view = "preview" if self._preview_open else "results" if self._results_open else "edit"
        _configure_changed(self._preview_toggle, text="返回编辑" if view != "edit" else "修改清单")
        self._layout_actions()
        if view == self._content_view:
            return
        self._content_view = view
        self._table.pack_forget()
        self._preview.pack_forget()
        self._details.pack_forget()
        self._review_heading.pack_forget()
        if view == "edit":
            if not self._filters.winfo_manager():
                self._filters.pack(in_=self._table, fill="x", padx=8, pady=(0, 6),
                                   after=self._header)
            self._table.pack(fill="both", expand=True)
        else:
            # The whole table is hidden. Keep its child anchors packed so DPI
            # changes can safely replay header before=filters while reviewing.
            if self._more_open:
                self._toggle_more()
            if self._custom_open:
                self._custom_open = False
                self._custom_form.pack_forget()
                self._custom_toggle.configure(text="＋ 自定义")
            self._review_heading.configure(text="修改清单与规则说明" if view == "preview" else "应用结果")
            self._review_heading.pack(fill="x", pady=(0, 6))
            (self._preview if view == "preview" else self._details).pack(fill="both", expand=True)

    def _update_preview(self):
        self._sync_content_view()
        if not self._preview_open or not self._drafts:
            return
        lines = []
        for item in self._changes:
            lines.extend([f"[{item['scope']}] {item['label']}", f"  原：{item['before']}",
                          f"  新：{'移除目标及其独立绑定' if item['removed'] else item['after']}", ""])
        text = "\n".join(lines) if lines else f"没有未保存修改。再次应用只处理当前位置：{self._scope}。"
        notices = self._default_notices.get(self._scope, [])
        if notices:
            text += "\n\n按用途分流说明：\n" + "\n".join(notices)
        preview = self._preflight(self._scope)
        rows = {row["id"]: row for row in proxy_routing.route_rows(self._drafts[self._scope])}
        def describe(service):
            row = rows[service]
            description = route_description(row, self._drafts[self._scope], self._catalog)
            return f"{row['label']}（{description['profile']}）"
        text += ("\n\n规则覆盖 / 生效范围（当前位置草稿，非运行态）：\n"
                 "同一目标以自定义设置为准；子域名 / 更小网段优先。域名规则排在 IP 规则之前。\n"
                 "沿用原规则不等于直连；直连需目标设备自身网络可达，不能与严格隐私同时启用。")
        for notice in preview["overlaps"][:40]:
            label = "覆盖" if notice["kind"] == "override" else f"子范围例外于 {notice['parent']}"
            text += f"\n• {notice['target']}：{label}；{describe(notice['shadowed'])} → {describe(notice['winner'])}"
        if len(preview["overlaps"]) > 40:
            text += f"\n另有 {len(preview['overlaps']) - 40} 处覆盖 / 例外，请缩小目标范围后逐项核对。"
        if not preview["overlaps"]:
            text += "\n未发现不同出口间的重叠规则。"
        if preview["privacy_conflict"]:
            text += "\n无法应用：直连目标与该设备的严格隐私模式冲突，请调整后再保存。"
        elif preview["direct"] and self._contexts.get(self._scope, {}).get("strict_privacy") is None:
            text += "\n直连依赖目标设备自身网络；远端严格隐私状态将在应用时核验。"
        if self._contexts.get(self._scope, {}).get("_authority_missing"):
            text += "\n本机尚无此服务器的分流记录。若远端已有分流，请先恢复本地记录，避免用空白草稿重建。"
        self._preview.configure(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", safe_feedback_text(text))
        self._preview.configure(state="disabled")

    def _preflight(self, scope):
        draft = self._drafts[scope]
        strict = self._contexts.get(scope, {}).get("strict_privacy")
        previous = self._preflight_cache.get(scope)
        if previous is None or previous[:2] != (draft, strict):
            result = route_preflight(draft, strict_privacy=strict)
            self._preflight_cache[scope] = (copy.deepcopy(draft), strict, result)
        return self._preflight_cache[scope][2]

    def _recover_routes(self, *, discard_draft=False):
        if self._busy or not self._recoverer or self._scope not in self._drafts:
            return
        if not discard_draft and self._drafts[self._scope] != self._originals[self._scope]:
            ConfirmDialog(self, title="恢复本地分流记录", message="将丢弃当前位置的未保存草稿并读取远端记录；不改变远端代理。",
                          on_confirm=lambda: self._recover_routes(discard_draft=True))
            return
        scope = self._scope
        self._busy = True
        self._set_editable(False)
        self._status.configure(text="正在读取远端分流记录，仅恢复本机记录，不改变远端代理…", text_color=COLORS["muted"])
        def run():
            try:
                self._queue.put(("recovered", (scope, self._recoverer(scope))))
            except Exception as exc:
                self._queue.put(("recovery_error", safe_feedback_text(str(exc))))
        self._start_worker(run, "service-routes-recover")

    def _layout_actions(self, event=None):
        width = (event.width if event else self._actions.winfo_width()) / self._actions._get_widget_scaling()
        columns = 3 if width >= 330 else 2
        buttons = []
        if self._preview_open or self._results_open:
            buttons.append(self._back_button)
        elif self._scope in self._drafts and self._drafts[self._scope] != self._originals[self._scope]:
            buttons.append(self._reset_button)
        buttons.extend((self._close_button, self._save_button))
        columns = min(columns, len(buttons))
        signature = (columns, tuple(buttons))
        if signature == self._action_columns:
            return
        self._action_columns = signature
        for button in (self._reset_button, self._back_button, self._close_button, self._save_button):
            button.grid_forget()
        for col in range(4):
            self._actions.grid_columnconfigure(col, weight=1 if col < columns else 0, uniform="route-actions" if col < columns else "")
        for index, button in enumerate(buttons):
            span = columns if index == len(buttons) - 1 and index % columns == 0 else 1
            button.grid(row=index // columns, column=index % columns, columnspan=span, sticky="ew",
                        padx=(0, 8) if span == 1 and index % columns < columns - 1 else 0, pady=(4, 0))

    def _layout_tools(self, event=None):
        width = (event.width if event else self._tools_row.winfo_width()) / self._tools_row._get_widget_scaling()
        columns = 4 if width >= 360 else 2
        if columns == self._tools_columns:
            return
        self._tools_columns = columns
        for col in range(6):
            self._tools_row.grid_columnconfigure(
                col, weight=1 if col < columns else 0, uniform="route-tools" if col < columns else "",
            )
        for index, button in enumerate((self._custom_toggle, self._bulk_button, self._more_toggle, self._preview_toggle)):
            button.grid(row=index // columns, column=index % columns, sticky="ew",
                        padx=(0, 8) if index % columns < columns - 1 else 0, pady=(2, 2))

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

    def _legacy_cleanup_protected(self):
        return self._manually_edited[self._scope] | {key for key in self._rows if self._row_changed(key)}

    def _update_legacy_cleanup(self):
        """Detection is read-only; absent provenance never authorizes a write."""
        candidates = legacy_social_route_candidates(
            self._drafts[self._scope], self._catalog, protected_services=self._legacy_cleanup_protected(),
        )
        self._legacy_cleanup_button.configure(
            text=f"整理旧版分流（{len(candidates)}）" if candidates else "整理旧版分流",
            state="normal" if candidates and not self._busy else "disabled",
        )
        return candidates

    def _cleanup_legacy_routes(self):
        if self._busy or self._closed or self._scope not in self._drafts:
            return
        before = self._drafts[self._scope]
        draft, notices = cleanup_legacy_social_routes(
            before, self._catalog, protected_services=self._legacy_cleanup_protected(),
        )
        changed = {service for service, profile in draft["service_profile_bindings"].items()
                   if before["service_profile_bindings"].get(service) != profile}
        self._drafts[self._scope] = draft
        self._manually_edited[self._scope].update(changed)
        self._default_notices[self._scope] = notices
        self._preview_open = True
        self._clear_filters()
        self._render()
        self._changed()
        self._details.pack_forget()
        self._status.configure(
            text=(f"已整理 {len(changed)} 项疑似旧版分流，仅修改当前位置草稿。"
                  "旧版未记录来源，可能是手动选择；请核对清单，再保存并应用。") if changed else
                 "没有可自动整理的旧版分流，原选择已保留；请在修改清单查看原因。",
            text_color=COLORS["accent"] if changed else COLORS["warning"],
        )

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
            text=f"已补齐 {added} 项草稿：AI 服务优先家宽，其余内置网站（含 X/Reddit）优先非家宽。"
                 "保留已有绑定、已关闭目标和手动修改；保存并应用后生效。",
            text_color=COLORS["accent"] if added else COLORS["warning"],
        )

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
                raw = {scope: self._loader(scope) for scope in self._scopes}
                preferences = {scope: proxy_routing.route_snapshot(value) for scope, value in raw.items()}
                contexts = {scope: {"strict_privacy": value.get("strict_privacy"),
                                    "_authority_missing": value.get("_authority_missing", False)}
                            for scope, value in raw.items()}
                self._queue.put(("loaded", (preferences, self._catalog_loader(), contexts)))
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
            self._originals, self._catalog, self._contexts = payload
            self._drafts = copy.deepcopy(self._originals)
            self._busy = False
            preset_requested = self._preset_requested
            self._preset_requested = False
            # Viewing saved routes is read-only. Recommendations are generated
            # only by an explicit preset/default action, never on load or browse.
            self._render()
            if self._initial_service in self._rows:
                self._search.set(self._rows[self._initial_service]["label"])
            self._set_editable(True)
            self._changed()
            if preset_requested:
                self.show_preset()
        elif event in ("catalog", "catalog_error"):
            self._busy = False
            if event == "catalog":
                self._catalog = payload
                self._render()
            self._set_editable(True)
            self._changed()
            if event == "catalog_error":
                self._status.configure(text=f"缓存读取失败：{payload}。已保留草稿和原有列表。", text_color=COLORS["danger"])
        elif event in ("recovered", "recovery_error"):
            self._busy = False
            if event == "recovered":
                scope, recovered = payload
                self._originals[scope] = proxy_routing.route_snapshot(recovered)
                self._drafts[scope] = copy.deepcopy(self._originals[scope])
                self._contexts[scope]["_authority_missing"] = False
                self._manually_edited[scope].clear()
                self._default_notices.pop(scope, None)
                self._render()
            self._set_editable(True)
            self._changed()
            self._status.configure(
                text="已恢复本地分流记录；远端代理未改变。缺失的订阅或节点需另行导入，核对后再应用。"
                if event == "recovered" else f"未恢复：{payload}。草稿和远端配置未改变。",
                text_color=COLORS["success"] if event == "recovered" else COLORS["danger"],
            )
        elif event == "applied":
            succeeded, errors = payload
            for scope, preferences, _message in succeeded:
                self._originals[scope] = preferences
                self._manually_edited[scope].clear()
                self._default_notices.pop(scope, None)
                self._contexts.setdefault(scope, {})["_authority_missing"] = False
            self._busy = False
            self._set_editable(True)
            self._preview_open = False
            self._changed()
            lines = [f"{scope}: {message}" for scope, _prefs, message in succeeded]
            lines.extend(f"{scope}: {error}" for scope, error in errors)
            self._status.configure(text=f"已处理 {len(succeeded)} 个位置，未成功 {len(errors)} 个。"
                                       + ("未成功项保留草稿，可再次应用重试。" if errors else ""),
                                   text_color=COLORS["warning"] if errors else COLORS["success"])
            self._details.configure(state="normal")
            self._details.delete("1.0", "end")
            self._details.insert("1.0", safe_feedback_text("\n".join(lines)))
            self._details.configure(state="disabled")
            self._results_open = True
            self._sync_content_view()
            if succeeded and self._on_saved:
                self._on_saved()
        else:
            self._busy = False
            self._preset_requested = False
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
        if service.startswith("custom:"):
            mapping[DEFAULT_PROFILE] = ""
        for profile in self._catalog:
            label = " ".join(safe_feedback_text(str(profile["name"])).split())
            if profile.get("network_type") in {"residential", "datacenter"}:
                label += " · " + ("家宽" if profile["network_type"] == "residential" else "非家宽")
            if not profile["nodes"]:
                label += " · 请先拉取"
            if label in (AUTO_PROFILE, DIRECT_PROFILE, ORIGINAL_RULES, RESUME_ROUTE):
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
                self._table, text="没有匹配的目标。试试清除筛选，或点击“＋ 自定义”添加目标。",
                text_color=COLORS["muted"], font=font(12), anchor="w", justify="left",
            )
            bind_wraplength(self._table, self._empty, padding=30)
        for info in infos:
            service = info["id"]
            if service in self._rows:
                row = self._rows[service]
                row["info"], row["label"] = info, info["label"]
                if row["enabled"].get() != info["enabled"]:
                    row["enabled"].set(info["enabled"])
                _configure_changed(row["name"], text=safe_feedback_text(info["label"]))
                self._refresh_row(service)
                continue
            profiles = self._profile_values(service)
            tile = ctk.CTkFrame(self._table, fg_color=COLORS["surface_alt"], corner_radius=8)
            target = ctk.CTkFrame(tile, fg_color="transparent")
            target.grid_columnconfigure(1, weight=1)
            enabled = ctk.BooleanVar(value=info["enabled"])
            name = ctk.CTkLabel(
                target, text=safe_feedback_text(info["label"]), font=font(12, "bold"),
                text_color=COLORS["text"], anchor="w", justify="left", width=1, height=22,
            )
            name.grid(row=0, column=1, sticky="ew")
            bind_wraplength(target, name, padding=30, min_width=130)
            state_label = ctk.CTkLabel(target, text="", font=font(10), text_color=COLORS["muted"], anchor="w", height=16)
            state_label.grid(row=1, column=1, sticky="w")
            profile_caption = ctk.CTkLabel(tile, text="访问线路", font=font(10), text_color=COLORS["muted"], anchor="w", height=16)
            profile_combo = ctk.CTkComboBox(
                tile, values=list(profiles), state="readonly", width=160,
                command=lambda label, key=service: self._select_profile(key, label), **combo_style(),
            )
            node_caption = ctk.CTkLabel(tile, text="节点与备用", font=font(10), text_color=COLORS["muted"], anchor="w", height=16)
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
                "tile": tile, "enabled": enabled, "always": info["always"],
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
        mode = draft.get("service_route_modes", {}).get(service)
        if not profile_id:
            if mode == "default":
                label = DEFAULT_PROFILE
            elif mode == "direct":
                label = DIRECT_PROFILE
        active = bool(row["enabled"].get())
        row["saved_choice"] = label
        if not active:
            label = ORIGINAL_RULES
        profile_labels = list(profiles)
        profile_labels.insert(1, DIRECT_PROFILE)
        if not row["always"]:
            profile_labels.insert(0, ORIGINAL_RULES)
            if not active:
                profile_labels.insert(1, RESUME_ROUTE)
        _configure_changed(row["profile"], state="disabled" if self._busy else "readonly", values=[
            *profile_labels, *([AUTO_PROFILE] if preferred_network_type(service) else []),
            *([MISSING_PROFILE] if label == MISSING_PROFILE else []),
        ])
        if row["profile"].get() != label:
            row["profile"].set(label)
        nodes = self._node_values(profile_id)
        key = draft["service_node_bindings"].get(service, "")
        pool = draft.get("service_node_pools", {}).get(service, [])
        node_label = self._node_reverse_cache[profile_id].get(key, MISSING_NODE)
        row["nodes"] = nodes
        row["node"].set("无需代理节点" if mode == "direct" else
                        f"自选 {len(pool)} 个候选 · 自动切换" if pool else node_label if profile_id else label)
        _configure_changed(row["node"], state="normal" if active and profile_id and not self._busy else "disabled", text_color_disabled=COLORS["muted"])
        row["info"]["enabled"] = bool(row["enabled"].get())
        description = route_description(row["info"], draft, self._catalog)
        row["description"] = description
        dirty = self._row_changed(service)
        state = "自定义目标共用设置" if service == "custom" else ("专用线路" if active else "沿用代理范围")
        preferred = preferred_network_type(service)
        if not active:
            state += " · 原线路已保留"
        elif mode == "direct":
            state += " · 手动直连"
        elif preferred:
            state += " · 建议" + ("家宽" if preferred == "residential" else "非家宽")
        _configure_changed(row["state_label"], text=state + (" · 未保存" if dirty else ""),
                                      text_color=COLORS["accent"] if dirty else COLORS["muted"])
        # Full names stay visible here when the dropdown entry is too narrow.
        detail = description["hint"]
        if profile_id or description["inherited"]:
            detail = f'{description["profile"]} → {description["node"]} · {detail}'
        elif description["source_hint"] and not description["warning"]:
            detail = description["source_hint"]
        _configure_changed(row["detail"], text=detail, text_color=COLORS["warning"] if description["warning"] else COLORS["muted_soft"])
        _configure_changed(row["tile"], border_width=1 if dirty or description["warning"] else 0,
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
        for service, row in self._rows.items():
            bound = bool(row["enabled"].get() and self._drafts[self._scope]["service_profile_bindings"].get(service))
            details = self._more_open or row["description"]["warning"]
            layout = (self._narrow, self._table._get_widget_scaling(), bound, details)
            if row.get("layout") == layout:
                continue
            row["layout"] = layout
            tile = row["tile"]
            for widget in tile.winfo_children():
                widget.grid_forget()
            for col in range(4):
                tile.grid_columnconfigure(col, weight=0, minsize=0, uniform="")
            if self._narrow:
                tile.grid_columnconfigure((0, 1), weight=1, uniform="route-editor")
                row["target"].grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 2))
                profile_col, node_col, control_row, detail_row = 0, 1, 2, 3
                row["profile_caption"].grid(row=1, column=profile_col, sticky="w", padx=8, pady=(4, 0))
                if bound:
                    row["node_caption"].grid(row=1, column=node_col, sticky="w", padx=8, pady=(4, 0))
            else:
                tile.grid_columnconfigure(0, minsize=round(190 * self._table._get_widget_scaling()))
                tile.grid_columnconfigure((1, 2), weight=1, uniform="route-editor")
                row["target"].grid(row=0, column=0, sticky="ew", padx=12, pady=8)
                profile_col, node_col, control_row, detail_row = 1, 2, 0, 1
            row["profile"].grid(row=control_row, column=profile_col, columnspan=1 if bound else 2,
                                sticky="ew", padx=8, pady=(0, 8) if self._narrow else 8)
            if bound:
                row["node"].grid(row=control_row, column=node_col, sticky="ew", padx=8,
                                 pady=(0, 8) if self._narrow else 8)
            if details:
                row["detail"].grid(row=detail_row, column=profile_col, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
            if row["delete"]:
                row["delete"].grid(row=0, column=2 if self._narrow else 3, padx=(0, 8), pady=4)

    def _on_resize(self, event):
        narrow = event.width / self._table._get_widget_scaling() < 860
        self._narrow = narrow
        self._layout_rows()

    def _schedule_filter(self):
        if self._filter_after_id:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(120, self._filter_rows)
        self._update_bulk_action()

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
        visible = []
        visible_services = []
        for service, row in self._rows.items():
            aliases = "gpt chatgpt" if service == "openai" else ""
            description = row["description"]
            text = f"{service} {row['label']} {aliases} {description['profile']} {description['node']}".casefold()
            category = ("自定义" if service == "custom" or service.startswith("custom:")
                        else "AI 服务" if row["always"] else "网站")
            show = (not query or query in text) and (
                self._category == "全部" or self._category == category
                or self._category == "待修复" and description["warning"])
            if show:
                visible.append(row["tile"])
                visible_services.append(service)
        self._visible_services = tuple(visible_services)
        if tuple(visible) != getattr(self, "_visible_tiles", None):
            self._visible_tiles = tuple(visible)
            self._empty.pack_forget()
            for row in self._rows.values():
                row["tile"].pack_forget()
            for tile in visible:
                tile.pack(fill="x", pady=(0, 6), padx=2)
            if not visible:
                self._empty.pack(fill="x", padx=12, pady=24)
        _configure_changed(self._count_label, text=f"显示 {len(visible)} / {len(self._rows)} 项 · 选线路即启用；“沿用原规则”不等于直连")
        self._update_bulk_action()

    def _has_route_filter(self):
        return self._category != "全部" or bool(self._search.get().strip())

    def _update_bulk_action(self, *, enabled=True):
        filtered = self._has_route_filter()
        pending = self._filter_after_id is not None
        count = len(getattr(self, "_visible_services", ()))
        # A previously empty search must not block an immediate click after
        # editing the query. Opening the dialog flushes this pending filter;
        # never advertise the old result count while it is being recomputed.
        ready = enabled and not self._busy and not self._closed and bool(self._rows) and (pending or not filtered or count > 0)
        _configure_changed(self._bulk_button,
                           text="批量设置…" if pending else f"批量设置（{count}）" if filtered else "批量设置",
                           state="normal" if ready else "disabled")

    def _select_profile(self, service, label):
        if self._busy or service not in self._rows:
            return
        if label in (ORIGINAL_RULES, RESUME_ROUTE):
            if not self._rows[service]["always"]:
                # Suspend/resume only the rule, preserving the exact pin/pool
                # and inheritance mode for a reversible one-choice workflow.
                self._toggle(service, label == RESUME_ROUTE)
            return
        if label == AUTO_PROFILE:
            self._use_tagged_default(service)
            return
        profiles = self._profile_values(service)
        if self._busy or (label not in profiles and label != DIRECT_PROFILE):
            return
        self._manually_edited[self._scope].add(service)
        draft = self._drafts[self._scope]
        direct = label == DIRECT_PROFILE
        profile_id = "" if direct else profiles[label]
        modes = draft.setdefault("service_route_modes", {})
        if direct:
            modes[service] = "direct"
        elif label == DEFAULT_PROFILE:
            modes[service] = "default"
        else:
            modes.pop(service, None)
        if draft["service_profile_bindings"].get(service, "") == profile_id:
            if not self._rows[service]["always"]:
                self._toggle(service, True)
            else:
                self._changed(service)
            return
        had_fixed_node = bool(draft["service_node_bindings"].get(service) or draft.get("service_node_pools", {}).get(service))
        if profile_id:
            draft["service_profile_bindings"][service] = profile_id
        else:
            draft["service_profile_bindings"].pop(service, None)
        draft["service_node_bindings"].pop(service, None)
        draft.get("service_node_pools", {}).pop(service, None)
        if not self._rows[service]["always"]:
            self._toggle(service, True)
        else:
            self._changed(service)
        if had_fixed_node:
            self._status.configure(text="线路已更改，原固定节点或候选池已从草稿解除。请确认新的访问线路后再保存。",
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
        self._results_open = False
        count = sum(self._drafts[scope] != self._originals[scope] for scope in self._scopes)
        self._changes = route_changes(self._originals, self._drafts, self._catalog)
        keys = ([service] + [key for key in self._rows if key.startswith("custom:") and service == "custom"]) if service else self._rows
        for key in keys:
            self._refresh_row(key)
        self._layout_rows()
        self._filter_rows()
        self._reset_button.configure(state="normal" if not self._busy and self._drafts[self._scope] != self._originals[self._scope] else "disabled")
        self._save_button.configure(text=f"保存并应用（{count}）" if count > 1 else "保存并应用",
                                    state="normal" if count and not self._busy else "disabled")
        self._reapply_button.configure(state="normal" if not count and not self._busy else "disabled")
        if count:
            self._details.pack_forget()
        self._update_preview()
        self._status.configure(text=f"待保存 · {count} 个位置 · {len(self._changes)} 项目标变化（含继承影响）" if count else "无未保存修改",
                               text_color=COLORS["accent"] if count else COLORS["muted"])
        if self._default_notices.get(self._scope):
            missing = sum("未分配：" in notice for notice in self._default_notices[self._scope])
            if missing:
                self._status.configure(text=self._status.cget("text") + " 部分用途未分配，请点“修改清单”查看。",
                                       text_color=COLORS["warning"])
        candidates = self._update_legacy_cleanup()
        preflight = self._preflight(self._scope)
        if preflight["overlaps"] or preflight["privacy_conflict"]:
            self._status.configure(
                text=self._status.cget("text") + (" 直连与严格隐私冲突，应用前需调整。" if preflight["privacy_conflict"] else
                                                 f" · {len(preflight['overlaps'])} 处规则覆盖 / 例外，请检查修改。"),
                text_color=COLORS["warning"],
            )
        if self._recovery_button:
            missing = self._contexts.get(self._scope, {}).get("_authority_missing")
            self._recovery_button.configure(state="normal" if missing and not self._busy else "disabled")
            if missing:
                self._status.configure(text="本机缺少该服务器分流记录；已有部署请先恢复，首次部署可手动设置后应用。",
                                       text_color=COLORS["warning"])
        if candidates:
            self._status.configure(
                text=self._status.cget("text") + f" 检测到 {len(candidates)} 项疑似旧版 X/Reddit 家宽绑定；"
                     "可在“更多 / 说明 → 整理旧版分流”中核对。",
                text_color=COLORS["warning"],
            )

    def _switch_scope(self, scope):
        if not self._busy and scope in self._drafts:
            self._scope = scope
            self._scope_combo.set(scope)
            self._render()
            self._changed()

    def _open_copy_dialog(self):
        if self._busy or len(self._scopes) < 2:
            return
        if self._scope_copy_dialog and self._scope_copy_dialog.winfo_exists():
            self._scope_copy_dialog.lift()
            return
        from ui.dialogs.route_selection_dialogs import RouteScopeCopyDialog

        source = self._scope
        self._scope_copy_dialog = RouteScopeCopyDialog(
            self, source=source, scopes=[scope for scope in self._scopes if scope != source],
            dirty_scopes={scope for scope in self._scopes if self._drafts[scope] != self._originals[scope]},
            on_copy=lambda targets: self._copy_to_scopes(targets, source=source),
        )

    def _open_bulk_dialog(self):
        if self._busy or self._closed:
            return
        if self._bulk_dialog and self._bulk_dialog.winfo_exists():
            self._bulk_dialog.lift()
            return
        from ui.dialogs.service_route_bulk_dialog import RouteBulkDialog

        # Flush the debounced search before capturing this explicit action's
        # targets. A just-entered query must never select the previous results.
        self._filter_rows()
        initial_services = self._visible_services if self._has_route_filter() else ()
        if self._has_route_filter() and not initial_services:
            return
        scope = self._scope
        self._bulk_dialog = RouteBulkDialog(
            self, rows=proxy_routing.route_rows(self._drafts[scope]), catalog=self._catalog,
            initial_services=initial_services, scope_label=scope,
            on_apply=lambda services, operation, **choices: self._accept_bulk_edit(scope, services, operation, **choices),
        )

    def show_preset(self):
        """Open the preset entry point without reloading or discarding a draft."""
        if self._closed:
            return
        if self._busy:
            if not self._drafts:
                self._preset_requested = True
                self._status.configure(text="正在读取分流和订阅，完成后打开智能方案…", text_color=COLORS["muted"])
            else:
                self._status.configure(text="分流操作正在进行，请完成后再打开智能方案。", text_color=COLORS["warning"])
            return
        if self._scope not in self._drafts:
            return
        modal = self.grab_current()
        if modal is not None and modal not in (self, self._preset_dialog):
            modal.lift()
            self._status.configure(text="请先完成或关闭当前子窗口，再打开智能方案；已有草稿已保留。",
                                   text_color=COLORS["warning"])
            return
        self._open_preset_dialog()

    def _open_preset_dialog(self):
        if self._busy or self._closed or self._scope not in self._drafts:
            return
        if self._preset_dialog and self._preset_dialog.winfo_exists():
            self._preset_dialog.lift()
            return
        from ui.dialogs.route_preset_dialog import RoutePresetDialog

        scope = self._scope
        original = copy.deepcopy(self._drafts[scope])
        self._preset_dialog = RoutePresetDialog(
            self, scope=scope, preferences=original, catalog=self._catalog,
            protected_services=self._manually_edited[scope],
            strict_privacy=self._contexts.get(scope, {}).get("strict_privacy"),
            on_accept=lambda **choices: self._accept_preset(scope, original, **choices),
            on_manage_sources=self._open_subscription_tags,
        )

    def _accept_preset(self, scope, original, *, expected_plan, **choices):
        if self._busy or self._closed or scope != self._scope or self._drafts.get(scope) != original:
            raise ValueError("位置或草稿已变化，请重新打开智能预设；未覆盖修改。")
        from core.route_presets import plan_route_preset

        plan = plan_route_preset(
            self._drafts[scope], self._catalog, **choices, protected_services=self._manually_edited[scope],
            strict_privacy=self._contexts.get(scope, {}).get("strict_privacy"),
        )
        if plan != expected_plan:
            raise ValueError("订阅缓存或分流策略已变化，请重新打开预设核对；原草稿未修改。")
        self._drafts[scope] = plan["draft"]
        self._manually_edited[scope].update(plan["changed_services"])
        self._default_notices[scope] = plan["notices"]
        # The preset already has a before/after review. Return to the editable
        # list instead of forcing a second review page before the single Apply.
        self._preview_open = self._results_open = False
        self._clear_filters()
        self._render()
        self._changed()
        self._status.configure(text=f"已生成 {len(plan['changed_services'])} 项预设草稿；可继续手动调整，保存并应用后生效。",
                               text_color=COLORS["accent"])

    def _accept_bulk_edit(self, scope, services, operation, **choices):
        if self._busy or self._closed or scope != self._scope:
            raise ValueError("当前位置已变化，请重新打开批量设置")
        from ui.dialogs.service_route_bulk_dialog import apply_route_batch

        draft, notices = apply_route_batch(self._drafts[scope], self._catalog, services, operation, **choices)
        self._drafts[scope] = draft
        self._manually_edited[scope].update(services)
        self._default_notices[scope] = notices
        self._preview_open = False
        self._render()
        self._changed()

    def _copy_to_scopes(self, targets, *, source=None):
        if self._busy or self._closed:
            return
        if source is not None and source != self._scope:
            self._status.configure(text="来源位置已变化，未复制草稿；请重新打开复制窗口。", text_color=COLORS["warning"])
            return
        for scope in dict.fromkeys(targets):
            if scope in self._drafts and scope != self._scope:
                self._drafts[scope] = copy.deepcopy(self._drafts[self._scope])
                self._manually_edited[scope] = set(self._manually_edited[self._scope])
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
                       self._tags_button, self._tag_routes_button, self._bulk_button, self._preset_button, self._legacy_cleanup_button,
                       self._recovery_button, self._preview_toggle, self._reapply_button):
            if button:
                button.configure(state=state)
        self._scope_combo.configure(state="readonly" if enabled and len(self._scopes) > 1 else "disabled")
        self._custom_entry.configure(state=state)
        self._custom_toggle.configure(state=state)
        for service, row in self._rows.items():
            _configure_changed(row["profile"], state="readonly" if enabled else "disabled")
            if row["delete"]:
                _configure_changed(row["delete"], state=state)
            self._refresh_row(service)
        if self._drafts:
            self._update_legacy_cleanup()
        pending = any(value != self._originals.get(scope) for scope, value in self._drafts.items())
        self._save_button.configure(state="normal" if enabled and pending else "disabled")
        self._reapply_button.configure(state="normal" if enabled and self._drafts and not pending else "disabled")
        self._update_bulk_action(enabled=enabled)

    def _reapply_current(self):
        if self._busy or self._closed or not self._drafts:
            return
        if any(value != self._originals.get(scope) for scope, value in self._drafts.items()):
            self._status.configure(text="请先保存或撤销未保存修改，再重新应用当前位置。", text_color=COLORS["warning"])
            return
        # Reuse every normal apply guard, including missing remote records,
        # strict privacy and narrowed AI candidate scope; never bypass them.
        self._apply()

    def _apply(self, *, allow_missing=False, approved_scope_change=None):
        if self._closed or self._busy or not self._drafts:
            return
        pending = {scope: copy.deepcopy(value) for scope, value in self._drafts.items()
                   if value != self._originals[scope]}
        if not pending:
            # Allow explicitly reapplying refreshed subscription caches.
            pending[self._scope] = copy.deepcopy(self._drafts[self._scope])
        originals = copy.deepcopy(self._originals)
        signature = (pending, originals)
        if approved_scope_change is not None and approved_scope_change != signature:
            self._status.configure(text="草稿或原配置已变化，旧的范围确认已失效；请重新核对并保存。", text_color=COLORS["warning"])
            return
        conflicts = [scope for scope in pending if self._preflight(scope)["privacy_conflict"]]
        if conflicts:
            self._preview_open = True
            self._update_preview()
            self._status.configure(text="未应用：以下位置的直连与严格隐私冲突：" + "、".join(conflicts),
                                   text_color=COLORS["danger"])
            return
        expansions = []
        for scope, draft in pending.items():
            labels = {row["id"]: row["label"] for row in proxy_routing.route_rows(draft)}
            expansions.extend(f"{scope} · {labels[service]}" for service in
                              proxy_routing.automatic_scope_expansions(originals[scope], draft))
        if expansions and approved_scope_change is None:
            if self._scope_confirm_dialog and self._scope_confirm_dialog.winfo_exists():
                self._scope_confirm_dialog.lift()
                return
            self._scope_confirm_dialog = ConfirmDialog(
                self, title="确认放宽节点切换范围", message=safe_feedback_text(
                    "以下目标将解除固定节点或自选候选限制，改用订阅自动或默认线路，可能跨国家；"
                    "订阅刷新也可能引入新候选。\n\n" + "\n".join(expansions)
                    + "\n\n不接受范围扩大，请取消并选择固定节点或自选候选。此确认不会验证出口国家。"),
                on_confirm=lambda: self._apply(allow_missing=allow_missing, approved_scope_change=signature))
            self._scope_confirm_dialog.geometry("560x400")
            center_window(self._scope_confirm_dialog, self)
            return
        missing = [scope for scope in pending if self._contexts.get(scope, {}).get("_authority_missing")]
        if missing and not allow_missing:
            ConfirmDialog(self, title="确认重建分流规则", message=safe_feedback_text(
                "本机尚无这些服务器的分流记录：" + "、".join(missing)
                + "。若远端已有分流，建议取消并先恢复记录。继续将按当前草稿重建远端规则，而不是合并未知旧规则。"),
                on_confirm=lambda: self._apply(allow_missing=True, approved_scope_change=signature))
            return
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
        self._preset_requested = False
        for dialog in (self._node_dialog, self._scope_copy_dialog, self._tags_dialog, self._bulk_dialog,
                       self._preset_dialog, self._scope_confirm_dialog):
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
