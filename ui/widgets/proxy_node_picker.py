import customtkinter as ctk

from core.lazy_imports import LazyModule
from ui.tabs.tab_visibility import is_active_tab
from ui.theme import COLORS, button_style, combo_style, font, input_style, recent_user_scroll


remote_proxy = LazyModule("core.remote_proxy")


class ProxyNodePicker(ctk.CTkFrame):
    """Searchable, scrollable picker for large proxy subscriptions."""

    FILTER_OPTIONS = ("全部", "可连", "不可连", "未测速")
    REGION_ALL = "全部地区"
    QUALITY_OPTIONS = ("全部质量", "家宽高质", "家宽/运营商", "低风险", "机房/商宽", "代理风险", "未测质量")
    MAX_VISIBLE_ROWS = 10
    VISIBLE_ROWS_STEP = 12
    RENDER_BATCH_SIZE = 1
    RENDER_BATCH_DELAY_MS = 95
    SCROLL_IDLE_RENDER_MS = 520

    def __init__(self, master, on_select=None, on_scope_change=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_select = on_select
        self._on_scope_change = on_scope_change
        self._nodes = []
        self._latency_results = {}
        self._quality_results = {}
        self._node_meta = {}
        self._summary_counts = {
            "ok": 0,
            "measured": 0,
            "quality": 0,
            "high_quality": 0,
        }
        self._selected_key = ""
        self._checked_keys = set()
        self._enabled = True
        self._render_after_id = None
        self._render_batch_after_id = None
        self._render_generation = 0
        self._render_plan_pending = False
        self._render_deferred = False
        self._last_match_count = 0
        self._last_visible_count = 0
        self._visible_limit = self.MAX_VISIBLE_ROWS
        self._metadata_version = 0
        self._filter_cache_key = None
        self._filter_cache_nodes = ()
        self._search_entry = None
        self._filter_combo = None
        self._region_combo = None
        self._quality_combo = None
        self._filter_reset_button = None
        self._batch_buttons = []
        self._scope_label = None
        self._summary_label = None
        self._list_frame = None
        self._visible_group_headers = []
        self._build_ui()

    def _build_ui(self):
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x")
        toolbar.grid_columnconfigure(0, weight=1)

        self._search_entry = ctk.CTkEntry(
            toolbar,
            placeholder_text="搜索节点名、地区、类型、服务器",
            **input_style(),
        )
        self._search_entry.grid(row=0, column=0, columnspan=4, sticky="ew")
        self._search_entry.bind("<KeyRelease>", lambda _event: self._request_render_nodes(120, reset_limit=True))

        filter_bar = ctk.CTkFrame(toolbar, fg_color="transparent")
        filter_bar.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        ctk.CTkLabel(
            filter_bar,
            text="连通",
            text_color=COLORS["muted_soft"],
            font=font(11),
        ).pack(side="left", padx=(0, 4))
        self._filter_combo = ctk.CTkComboBox(
            filter_bar,
            values=list(self.FILTER_OPTIONS),
            width=88,
            command=lambda _value: self._request_render_nodes(20, reset_limit=True),
            **combo_style(),
        )
        self._filter_combo.set("全部")
        self._filter_combo.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            filter_bar,
            text="地区",
            text_color=COLORS["muted_soft"],
            font=font(11),
        ).pack(side="left", padx=(0, 4))
        self._region_combo = ctk.CTkComboBox(
            filter_bar,
            values=[self.REGION_ALL],
            width=106,
            command=lambda _value: self._request_render_nodes(20, reset_limit=True),
            **combo_style(),
        )
        self._region_combo.set(self.REGION_ALL)
        self._region_combo.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            filter_bar,
            text="质量",
            text_color=COLORS["muted_soft"],
            font=font(11),
        ).pack(side="left", padx=(0, 4))
        self._quality_combo = ctk.CTkComboBox(
            filter_bar,
            values=list(self.QUALITY_OPTIONS),
            width=110,
            command=lambda _value: self._request_render_nodes(20, reset_limit=True),
            **combo_style(),
        )
        self._quality_combo.set("全部质量")
        self._quality_combo.pack(side="left", padx=(0, 8))
        self._filter_reset_button = ctk.CTkButton(
            filter_bar,
            text="重置",
            width=58,
            command=self._reset_filters,
            **button_style("secondary", compact=True),
        )
        self._filter_reset_button.pack(side="left")

        scope_bar = ctk.CTkFrame(toolbar, fg_color="transparent")
        scope_bar.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        scope_bar.grid_columnconfigure(0, weight=1)

        self._scope_label = ctk.CTkLabel(
            scope_bar,
            text="批量范围: 全部 0 个节点",
            text_color=COLORS["muted_soft"],
            font=font(11, "bold"),
            anchor="w",
        )
        self._scope_label.grid(row=0, column=0, sticky="ew")

        match_button = ctk.CTkButton(
            scope_bar,
            text="全选匹配",
            width=84,
            command=lambda: self._set_matching_checked(True),
            **button_style("secondary", compact=True),
        )
        match_button.grid(row=0, column=1, sticky="e", padx=(8, 6))
        clear_button = ctk.CTkButton(
            scope_bar,
            text="清空匹配",
            width=84,
            command=lambda: self._set_matching_checked(False),
            **button_style("secondary", compact=True),
        )
        clear_button.grid(row=0, column=2, sticky="e")
        self._batch_buttons = [match_button, clear_button]

        self._summary_label = ctk.CTkLabel(
            self,
            text="暂无节点",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
        )
        self._summary_label.pack(fill="x", pady=(6, 5))

        self._list_frame = ctk.CTkScrollableFrame(
            self,
            height=222,
            fg_color=COLORS["field_bg"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
            scrollbar_button_color=COLORS["secondary"],
            scrollbar_button_hover_color=COLORS["secondary_hover"],
        )
        self._list_frame.pack(fill="x")

    def set_nodes(self, nodes, latency_results=None, selected_key: str = "", quality_results=None):
        self._nodes = list(nodes or [])
        self._latency_results = latency_results if isinstance(latency_results, dict) else {}
        self._quality_results = quality_results if isinstance(quality_results, dict) else {}
        self._visible_limit = self.MAX_VISIBLE_ROWS
        self._build_node_metadata()
        self._update_region_options()
        available_keys = {self._node_key(item) for item in self._nodes}
        if selected_key and selected_key in available_keys:
            self._selected_key = selected_key
        elif self._selected_key and self._selected_key not in available_keys:
            self._selected_key = ""
        if not self._selected_key and self._nodes:
            self._selected_key = self._node_key(self._nodes[0])
        if not self._nodes:
            self._selected_key = ""
        self._checked_keys.intersection_update(available_keys)
        self._render_nodes()

    def destroy(self):
        self._cancel_pending_render()
        self._cancel_incremental_render()
        self._render_deferred = False
        super().destroy()

    def _suspend_background_work(self):
        if self._render_after_id or self._render_batch_after_id or self._render_plan_pending:
            self._render_deferred = True
        self._cancel_pending_render()
        self._cancel_incremental_render()
        self._render_plan_pending = False
        self._update_summary_label()

    def _resume_background_work(self):
        if not self._render_deferred:
            return
        self._render_deferred = False
        self._request_render_nodes(20)

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if self._enabled == enabled:
            return
        self._enabled = enabled
        state = "normal" if self._enabled else "disabled"
        for widget in (
            self._search_entry,
            self._filter_combo,
            self._region_combo,
            self._quality_combo,
            self._filter_reset_button,
            *self._batch_buttons,
        ):
            if widget:
                try:
                    widget.configure(state=state)
                except Exception:
                    pass
        self._set_visible_rows_enabled(self._enabled)

    def selected_key(self) -> str:
        return self._selected_key

    def selected_item(self):
        for item in self._nodes:
            if self._node_key(item) == self._selected_key:
                return item
        return None

    def checked_items(self) -> list:
        if not self._checked_keys:
            return []
        return [item for item in self._nodes if self._node_key(item) in self._checked_keys]

    def filtered_items(self) -> list:
        return list(self._filtered_nodes())

    def batch_items(self) -> list:
        checked = self.checked_items()
        if checked:
            return checked
        filtered = self.filtered_items()
        if self._has_active_filters():
            return filtered
        return list(self._nodes)

    def batch_scope_label(self) -> str:
        checked_count = len(self._checked_keys)
        if checked_count:
            return f"已勾选 {checked_count} 个节点"
        if self._has_active_filters():
            return f"当前筛选 {self._current_filtered_count()} 个节点"
        return f"全部 {len(self._nodes)} 个节点"

    def select_by_key(self, node_key: str) -> bool:
        key = str(node_key or "")
        if not key:
            return False
        for item in self._nodes:
            if self._node_key(item) == key:
                if self._selected_key == key:
                    return True
                self._selected_key = key
                self._render_nodes()
                return True
        return False

    def _reset_filters(self):
        try:
            if self._search_entry:
                self._search_entry.delete(0, "end")
            if self._filter_combo:
                self._filter_combo.set("全部")
            if self._region_combo:
                self._region_combo.set(self.REGION_ALL)
            if self._quality_combo:
                self._quality_combo.set("全部质量")
        except Exception:
            pass
        self._visible_limit = self.MAX_VISIBLE_ROWS
        self._render_nodes()

    def _render_nodes(self):
        pending_render = self._render_after_id
        self._render_after_id = None
        if pending_render:
            try:
                self.after_cancel(pending_render)
            except Exception:
                pass
        self._cancel_incremental_render()
        if not is_active_tab(self):
            self._render_deferred = True
            self._render_plan_pending = False
            self._update_summary_label()
            return
        self._render_deferred = False
        self._render_generation += 1
        generation = self._render_generation
        self._render_plan_pending = True
        if not self._list_frame:
            self._render_plan_pending = False
            return
        self._visible_group_headers = []
        for child in self._list_frame.winfo_children():
            child.destroy()

        matches = self._filtered_nodes()
        self._last_match_count = len(matches)
        total = len(self._nodes)
        quality_count = int(self._summary_counts.get("quality") or 0)
        visible_limit = max(self.MAX_VISIBLE_ROWS, int(self._visible_limit or self.MAX_VISIBLE_ROWS))
        visible = matches[:visible_limit]
        selected_item = self.selected_item()
        if selected_item in matches and selected_item not in visible:
            visible = [selected_item] + visible[: max(0, visible_limit - 1)]
        self._last_visible_count = len(visible)

        self._update_summary_label(match_count=len(matches), visible_count=len(visible))
        self._update_scope_label()

        if not visible:
            self._render_plan_pending = False
            ctk.CTkLabel(
                self._list_frame,
                text=self._empty_message(total, quality_count),
                text_color=COLORS["muted"],
                font=font(12),
            ).pack(fill="x", padx=12, pady=16)
            self._emit_scope_change()
            return

        render_plan = []
        for region, group_items in self._group_visible_nodes(visible):
            render_plan.append(("header", region, group_items))
            for item in group_items:
                render_plan.append(("row", item, None))
        remaining = max(0, len(matches) - len(visible))
        if remaining:
            render_plan.append(("more", remaining, None))
        self._emit_scope_change()
        try:
            self._render_batch_after_id = self.after(
                45,
                lambda: self._render_plan_batch(generation, render_plan, 0),
            )
        except Exception:
            self._render_batch_after_id = None
            self._render_plan_batch(generation, render_plan, 0)

    def _render_plan_batch(self, generation: int, render_plan: list, start_index: int):
        if generation != self._render_generation or not self._list_frame:
            self._render_plan_pending = False
            return
        if not is_active_tab(self):
            self._render_batch_after_id = None
            self._render_deferred = True
            self._render_plan_pending = False
            self._update_summary_label(match_count=self._last_match_count, visible_count=self._last_visible_count)
            return
        if recent_user_scroll(self, idle_ms=self.SCROLL_IDLE_RENDER_MS):
            try:
                self._render_batch_after_id = self.after(
                    self.RENDER_BATCH_DELAY_MS,
                    lambda: self._render_plan_batch(generation, render_plan, start_index),
                )
            except Exception:
                self._render_batch_after_id = None
            return
        batch_size = self.RENDER_BATCH_SIZE
        end_index = min(len(render_plan), start_index + batch_size)
        for kind, payload, extra in render_plan[start_index:end_index]:
            if kind == "header":
                self._render_group_header(payload, extra)
            elif kind == "row":
                self._render_row(payload)
            else:
                self._render_more_footer(int(payload or 0))
        if end_index >= len(render_plan):
            self._render_batch_after_id = None
            self._render_plan_pending = False
            self._update_summary_label(match_count=self._last_match_count, visible_count=self._last_visible_count)
            return
        try:
            self._render_batch_after_id = self.after(
                self.RENDER_BATCH_DELAY_MS,
                lambda: self._render_plan_batch(generation, render_plan, end_index),
            )
        except Exception:
            self._render_batch_after_id = None
            self._render_plan_pending = False

    def _group_visible_nodes(self, items):
        groups = []
        current_region = None
        current_items = []
        for item in items:
            region = self._node_region(item)
            if current_items and region != current_region:
                groups.append((current_region, current_items))
                current_items = []
            current_region = region
            current_items.append(item)
        if current_items:
            groups.append((current_region, current_items))
        return groups

    def _render_group_header(self, region: str, items):
        metas = [self._metadata_for(item) for item in items]
        keys = [str(meta.get("key") or "") for meta in metas]
        checked = sum(1 for key in keys if key in self._checked_keys)
        ok_count = sum(1 for meta in metas if meta.get("latency_ok"))
        high_count = sum(1 for meta in metas if meta.get("quality_ai_ok"))
        header = ctk.CTkFrame(self._list_frame, fg_color=COLORS["surface_alt"], corner_radius=6)
        header.pack(fill="x", padx=5, pady=(6, 0))
        header.grid_columnconfigure(0, weight=1)
        label = ctk.CTkLabel(
            header,
            text=self._group_header_text(region, len(items), ok_count, high_count, checked),
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="w",
        )
        label.grid(row=0, column=0, sticky="ew", padx=(9, 8), pady=4)
        self._visible_group_headers.append(
            {
                "label": label,
                "keys": tuple(keys),
                "region": region,
                "total": len(items),
                "ok": ok_count,
                "high": high_count,
            }
        )
        ctk.CTkButton(
            header,
            text="全选",
            width=58,
            state="normal" if self._enabled else "disabled",
            command=lambda group_keys=tuple(keys): self._set_group_checked(group_keys, True),
            **button_style("secondary", compact=True),
        ).grid(row=0, column=1, sticky="e", padx=(0, 6), pady=5)
        ctk.CTkButton(
            header,
            text="清空",
            width=58,
            state="normal" if self._enabled else "disabled",
            command=lambda group_keys=tuple(keys): self._set_group_checked(group_keys, False),
            **button_style("secondary", compact=True),
        ).grid(row=0, column=2, sticky="e", padx=(0, 6), pady=5)

    def _render_row(self, item):
        meta = self._metadata_for(item)
        node_key = str(meta.get("key") or "")
        node = item.node
        selected = node_key == self._selected_key
        latency = meta.get("latency")
        latency_label = str(meta.get("latency_label") or "")
        latency_detail = str(meta.get("latency_detail") or "")
        latency_color = self._latency_color(latency)
        quality = meta.get("quality")
        quality_label = str(meta.get("quality_label") or "")
        quality_score = int(meta.get("quality_score") or 0)
        quality_ip = str(meta.get("quality_ip") or "")
        region = str(meta.get("region") or "其他")
        node_type = str(node.get("type") or "").upper()
        server = str(node.get("server") or "")
        port = str(node.get("port") or "")

        row = ctk.CTkFrame(
            self._list_frame,
            fg_color=COLORS["surface_alt"] if selected else COLORS["field_bg"],
            corner_radius=6,
        )
        row.pack(fill="x", padx=5, pady=(4, 0))
        row.grid_columnconfigure(2, weight=1)

        checked_var = ctk.BooleanVar(value=node_key in self._checked_keys)
        ctk.CTkCheckBox(
            row,
            text="",
            width=24,
            checkbox_width=16,
            checkbox_height=16,
            variable=checked_var,
            state="normal" if self._enabled else "disabled",
            command=lambda key=node_key, var=checked_var: self._toggle_checked(key, var.get()),
            text_color=COLORS["muted"],
            font=font(11),
        ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(7, 3), pady=6)

        ctk.CTkButton(
            row,
            text="当前" if selected else "使用",
            width=50,
            state="normal" if self._enabled else "disabled",
            command=lambda key=node_key: self._select(key),
            **button_style("primary" if selected else "secondary", compact=True),
        ).grid(row=0, column=1, rowspan=2, sticky="w", padx=(3, 7), pady=6)

        title = str(node.get("name") or item.display_name())
        title_label = ctk.CTkLabel(
            row,
            text=title,
            text_color=COLORS["text"],
            font=font(12, "bold"),
            anchor="w",
            justify="left",
            wraplength=680,
        )
        title_label.grid(row=0, column=2, sticky="ew", pady=(6, 0))

        meta_parts = [f"【{region}】", node_type, f"{server}:{port}" if port else server]
        if remote_proxy.proxy_node_quality_measured(quality):
            quality_part = f"{quality_label} {quality_score}"
            if quality_ip:
                quality_part += f" {quality_ip}"
            source_label = str(meta.get("quality_source_label") or "")
            if source_label and source_label != "未标明检测源":
                quality_part += f" 基于{source_label}"
            meta_parts.append(quality_part)
        elif quality:
            meta_parts.append(quality_label)
        if latency_detail:
            meta_parts.append(latency_detail)
        meta_label = ctk.CTkLabel(
            row,
            text=" · ".join(part for part in meta_parts if part),
            text_color=COLORS["muted"],
            font=font(11),
            anchor="w",
            justify="left",
            wraplength=680,
        )
        meta_label.grid(row=1, column=2, sticky="ew", pady=(0, 6))

        ctk.CTkLabel(
            row,
            text=latency_label,
            text_color=latency_color,
            font=font(12, "bold"),
            width=58,
            anchor="e",
        ).grid(row=0, column=3, rowspan=2, sticky="e", padx=(7, 8))

        ctk.CTkLabel(
            row,
            text=quality_label if quality else "未测质量",
            text_color=self._quality_color(quality),
            font=font(11, "bold"),
            width=76,
            anchor="e",
        ).grid(row=0, column=4, rowspan=2, sticky="e", padx=(0, 8))

    def _render_more_footer(self, remaining: int):
        footer = ctk.CTkFrame(self._list_frame, fg_color="transparent")
        footer.pack(fill="x", padx=5, pady=(8, 4))
        footer.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            footer,
            text=f"还有 {remaining} 个匹配节点",
            text_color=COLORS["muted"],
            font=font(11),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            footer,
            text="显示更多",
            width=82,
            state="normal" if self._enabled else "disabled",
            command=self._show_more_nodes,
            **button_style("secondary", compact=True),
        ).grid(row=0, column=1, sticky="e", padx=(8, 0))

    def _select(self, node_key: str):
        self._selected_key = node_key
        item = self.selected_item()
        self._render_nodes()
        if item and self._on_select:
            self._on_select(item)

    def _toggle_checked(self, node_key: str, checked: bool):
        if checked:
            self._checked_keys.add(node_key)
        else:
            self._checked_keys.discard(node_key)
        self._update_scope_label()
        self._update_summary_label()
        self._update_group_headers()
        self._emit_scope_change()

    def _set_group_checked(self, keys, checked: bool):
        if checked:
            self._checked_keys.update(keys)
        else:
            for key in keys:
                self._checked_keys.discard(key)
        self._render_nodes()

    def _show_more_nodes(self):
        self._visible_limit = max(self.MAX_VISIBLE_ROWS, int(self._visible_limit or self.MAX_VISIBLE_ROWS))
        self._visible_limit += self.VISIBLE_ROWS_STEP
        self._render_nodes()

    def _set_matching_checked(self, checked: bool):
        keys = [self._node_key(item) for item in self._filtered_nodes()]
        if checked:
            self._checked_keys.update(keys)
        else:
            for key in keys:
                self._checked_keys.discard(key)
        self._render_nodes()

    def _request_render_nodes(self, delay_ms: int = 60, reset_limit: bool = False):
        if reset_limit:
            self._visible_limit = self.MAX_VISIBLE_ROWS
        self._cancel_pending_render()
        self._cancel_incremental_render()
        try:
            self._render_after_id = self.after(max(1, int(delay_ms)), self._render_nodes)
        except Exception:
            self._render_nodes()

    def _cancel_pending_render(self):
        if not self._render_after_id:
            return
        try:
            self.after_cancel(self._render_after_id)
        except Exception:
            pass
        self._render_after_id = None

    def _cancel_incremental_render(self):
        if not self._render_batch_after_id:
            return
        try:
            self.after_cancel(self._render_batch_after_id)
        except Exception:
            pass
        self._render_batch_after_id = None

    def _update_scope_label(self):
        if not self._scope_label:
            return
        color = COLORS["accent"] if self._checked_keys else COLORS["warning"] if self._has_active_filters() else COLORS["muted_soft"]
        self._scope_label.configure(text=f"批量范围: {self.batch_scope_label()}", text_color=color)

    def _update_summary_label(self, match_count: int | None = None, visible_count: int | None = None):
        if not self._summary_label:
            return
        if match_count is None:
            match_count = self._last_match_count
        total = len(self._nodes)
        ok_count = int(self._summary_counts.get("ok") or 0)
        measured_count = int(self._summary_counts.get("measured") or 0)
        quality_count = int(self._summary_counts.get("quality") or 0)
        high_quality_count = int(self._summary_counts.get("high_quality") or 0)
        checked_count = len(self._checked_keys)
        suffix = ""
        if visible_count is not None and match_count > visible_count:
            suffix = f"；先显示 {visible_count} 个，可显示更多或继续筛选"
        if self._render_plan_pending:
            suffix += "；正在分批渲染"
        self._summary_label.configure(
            text=(
                f"节点 {total} 个；可连 {ok_count}；延迟 {measured_count}；"
                f"质量 {quality_count}；高质 {high_quality_count}；"
                f"勾选 {checked_count}；匹配 {match_count} 个{suffix}"
            )
        )

    def _group_header_text(self, region: str, total: int, ok_count: int, high_count: int, checked: int) -> str:
        return f"{region or '其他'} · {total} 个 · 可连 {ok_count} · 高质 {high_count} · 已选 {checked}"

    def _update_group_headers(self):
        alive_headers = []
        for header in self._visible_group_headers:
            label = header.get("label")
            try:
                if not label or not label.winfo_exists():
                    continue
                keys = tuple(header.get("keys") or ())
                checked = sum(1 for key in keys if key in self._checked_keys)
                label.configure(
                    text=self._group_header_text(
                        str(header.get("region") or ""),
                        int(header.get("total") or 0),
                        int(header.get("ok") or 0),
                        int(header.get("high") or 0),
                        checked,
                    )
                )
                alive_headers.append(header)
            except Exception:
                continue
        self._visible_group_headers = alive_headers

    def _emit_scope_change(self):
        if not self._on_scope_change:
            return
        try:
            self._on_scope_change()
        except Exception:
            pass

    def _filtered_nodes(self):
        query = self._search_text()
        mode = self._filter_combo.get() if self._filter_combo else "全部"
        if mode not in self.FILTER_OPTIONS:
            mode = "全部"
        region_filter = self._region_combo.get() if self._region_combo else self.REGION_ALL
        if not region_filter:
            region_filter = self.REGION_ALL
        quality_filter = self._quality_combo.get() if self._quality_combo else "全部质量"
        if quality_filter not in self.QUALITY_OPTIONS:
            quality_filter = "全部质量"
        cache_key = (self._metadata_version, query, mode, region_filter, quality_filter)
        if self._filter_cache_key == cache_key:
            return list(self._filter_cache_nodes)
        matches = []
        for item in self._nodes:
            meta = self._metadata_for(item)
            if region_filter and region_filter != self.REGION_ALL and self._node_region(item) != region_filter:
                continue
            latency_ok = bool(meta.get("latency_ok"))
            latency_measured = bool(meta.get("latency_measured"))
            if mode == "可连" and not latency_ok:
                continue
            if mode == "不可连" and (not latency_measured or latency_ok):
                continue
            if mode == "未测速" and latency_measured:
                continue
            if not self._quality_matches(quality_filter, meta):
                continue
            if query and query not in self._search_blob(item):
                continue
            matches.append(item)
        self._filter_cache_key = cache_key
        self._filter_cache_nodes = tuple(matches)
        return matches

    def _empty_message(self, total: int, quality_count: int) -> str:
        if total <= 0:
            return "暂无节点，请先拉取订阅"
        mode = self._quality_combo.get() if self._quality_combo else "全部质量"
        if mode and mode != "全部质量" and quality_count <= 0:
            return "暂无质量结果，可先点击“质量选优”"
        return "没有匹配的节点"

    def _search_text(self) -> str:
        if not self._search_entry:
            return ""
        return self._search_entry.get().strip().casefold()

    def _current_filtered_count(self) -> int:
        try:
            return len(self._filtered_nodes())
        except Exception:
            return int(self._last_match_count or 0)

    def _has_active_filters(self) -> bool:
        mode = self._filter_combo.get() if self._filter_combo else "全部"
        region_filter = self._region_combo.get() if self._region_combo else self.REGION_ALL
        quality_filter = self._quality_combo.get() if self._quality_combo else "全部质量"
        return bool(
            self._search_text()
            or mode != "全部"
            or (region_filter and region_filter != self.REGION_ALL)
            or quality_filter != "全部质量"
        )

    def _search_blob(self, item) -> str:
        return str(self._metadata_for(item).get("search_blob") or "")

    def _latency_for(self, item):
        return self._metadata_for(item).get("latency")

    def _quality_for(self, item):
        return self._metadata_for(item).get("quality")

    def _node_key(self, item) -> str:
        return str(self._metadata_for(item).get("key") or "")

    def _node_region(self, item) -> str:
        return str(self._metadata_for(item).get("region") or "其他")

    def _set_visible_rows_enabled(self, enabled: bool):
        if not self._list_frame:
            return
        state = "normal" if enabled else "disabled"

        def visit(widget):
            try:
                children = widget.winfo_children()
            except Exception:
                return
            for child in children:
                if isinstance(child, (ctk.CTkButton, ctk.CTkCheckBox)):
                    try:
                        child.configure(state=state)
                    except Exception:
                        pass
                visit(child)

        visit(self._list_frame)

    def _metadata_for(self, item) -> dict:
        meta = self._node_meta.get(id(item))
        if meta is not None:
            return meta
        return self._build_item_metadata(item)

    def _build_node_metadata(self):
        self._node_meta = {}
        counts = {
            "ok": 0,
            "measured": 0,
            "quality": 0,
            "high_quality": 0,
        }
        for item in self._nodes:
            meta = self._build_item_metadata(item)
            self._node_meta[id(item)] = meta
            if meta.get("latency_ok"):
                counts["ok"] += 1
            if meta.get("latency_measured"):
                counts["measured"] += 1
            if meta.get("quality_measured"):
                counts["quality"] += 1
            if meta.get("quality_ai_ok"):
                counts["high_quality"] += 1
        self._summary_counts = counts
        self._metadata_version += 1
        self._invalidate_filter_cache()

    def _invalidate_filter_cache(self):
        self._filter_cache_key = None
        self._filter_cache_nodes = ()

    def _build_item_metadata(self, item) -> dict:
        node = item.node
        try:
            node_key = remote_proxy.proxy_subscription_node_key(item)
        except Exception:
            node_key = f"invalid:{item.index}:{id(item)}"
        try:
            region = remote_proxy.proxy_subscription_node_region(item)
        except Exception:
            region = "其他"
        latency = self._latency_results.get(node_key)
        quality = self._quality_results.get(node_key)
        latency_label = remote_proxy.proxy_node_latency_label(latency)
        latency_detail = remote_proxy.proxy_node_latency_detail(latency)
        quality_label = remote_proxy.proxy_node_quality_label(quality)
        quality_detail = remote_proxy.proxy_node_quality_detail(quality)
        quality_ip_type = remote_proxy.proxy_node_quality_ip_type(quality)
        quality_ip = remote_proxy.proxy_node_quality_ip(quality)
        quality_source_label = remote_proxy.proxy_node_quality_source_label(quality)
        quality_score = remote_proxy.proxy_node_quality_score(quality)
        quality_risk = remote_proxy.proxy_node_quality_risk_score(quality)
        quality_measured = remote_proxy.proxy_node_quality_measured(quality)
        quality_ai_ok = remote_proxy.proxy_node_quality_for_ai_proxy_ok(quality)
        static_parts = [
            str(item.index),
            str(node.get("name") or ""),
            str(node.get("type") or ""),
            str(node.get("server") or ""),
            region,
        ]
        search_parts = [
            *static_parts,
            latency_label,
            latency_detail,
            quality_label,
            quality_detail,
            quality_ip_type,
            quality_ip,
        ]
        meta = {
            "key": node_key,
            "region": region,
            "search_static": " ".join(static_parts).casefold(),
            "search_blob": " ".join(part for part in search_parts if part).casefold(),
            "latency": latency,
            "latency_label": latency_label,
            "latency_detail": latency_detail,
            "latency_ok": remote_proxy.proxy_node_latency_ok(latency),
            "latency_measured": latency is not None,
            "quality": quality,
            "quality_label": quality_label,
            "quality_detail": quality_detail,
            "quality_ip_type": quality_ip_type,
            "quality_ip": quality_ip,
            "quality_source_label": quality_source_label,
            "quality_measured": quality_measured,
            "quality_ai_ok": quality_ai_ok,
            "quality_score": quality_score,
            "quality_risk": quality_risk,
            "quality_label_text": quality_label.casefold(),
            "quality_ip_type_text": quality_ip_type.casefold(),
        }
        self._node_meta[id(item)] = meta
        return meta

    def _latency_color(self, result) -> str:
        if remote_proxy.proxy_node_latency_ok(result):
            return COLORS["success"]
        if result is None:
            return COLORS["muted_soft"]
        return COLORS["danger"]

    def _quality_color(self, result) -> str:
        if not remote_proxy.proxy_node_quality_measured(result):
            if result and "解析失败" in remote_proxy.proxy_node_quality_label(result):
                return COLORS["danger"]
            return COLORS["muted_soft"]
        if remote_proxy.proxy_node_quality_for_ai_proxy_ok(result):
            return COLORS["success"]
        label = remote_proxy.proxy_node_quality_label(result)
        if any(marker in label for marker in ("代理", "机房", "高风险", "冲突")):
            return COLORS["danger"]
        if remote_proxy.proxy_node_quality_score(result) >= 65:
            return COLORS["warning"]
        return COLORS["muted"]

    def _quality_matches(self, mode: str, meta: dict) -> bool:
        if not mode or mode == "全部质量":
            return True
        measured = bool(meta.get("quality_measured"))
        if mode == "未测质量":
            return not measured
        if not measured:
            return False
        label_text = str(meta.get("quality_label_text") or "")
        ip_type_text = str(meta.get("quality_ip_type_text") or "")
        quality_score = int(meta.get("quality_score") or 0)
        risk = meta.get("quality_risk")
        if mode == "家宽高质":
            return bool(meta.get("quality_ai_ok"))
        if mode == "家宽/运营商":
            return any(marker in label_text or marker in ip_type_text for marker in ("家宽", "家庭", "住宅", "residential", "home", "broadband", "运营商/宽带"))
        if mode == "低风险":
            return quality_score >= 75 and (risk is None or risk <= 35)
        if mode == "机房/商宽":
            return any(marker in label_text or marker in ip_type_text for marker in ("机房", "idc", "hosting", "datacenter", "business", "商宽", "企业"))
        if mode == "代理风险":
            return quality_score <= 40 or any(
                marker in label_text or marker in ip_type_text
                for marker in ("代理", "vpn", "tor", "proxy", "匿名", "高风险", "冲突")
            )
        return True

    def _update_region_options(self):
        if not self._region_combo:
            return
        regions = {self._node_region(item) for item in self._nodes}
        ordered = [region for region in remote_proxy.PROXY_REGION_ORDER if region in regions]
        ordered.extend(sorted(regions - set(ordered)))
        values = [self.REGION_ALL] + ordered
        current = self._region_combo.get() or self.REGION_ALL
        self._region_combo.configure(values=values)
        if current not in values:
            self._region_combo.set(self.REGION_ALL)
