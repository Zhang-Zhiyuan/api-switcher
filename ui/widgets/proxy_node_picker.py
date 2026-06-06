import customtkinter as ctk

from core import remote_proxy
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font, input_style


class ProxyNodePicker(ctk.CTkFrame):
    """Searchable, scrollable picker for large proxy subscriptions."""

    FILTER_OPTIONS = ("全部", "可连", "不可连", "未测速")
    REGION_ALL = "全部地区"
    QUALITY_OPTIONS = ("全部质量", "家宽高质", "家宽/运营商", "低风险", "机房/商宽", "代理风险", "未测质量")
    MAX_VISIBLE_ROWS = 120

    def __init__(self, master, on_select=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_select = on_select
        self._nodes = []
        self._latency_results = {}
        self._quality_results = {}
        self._selected_key = ""
        self._checked_keys = set()
        self._enabled = True
        self._search_entry = None
        self._filter_combo = None
        self._region_combo = None
        self._quality_combo = None
        self._batch_buttons = []
        self._summary_label = None
        self._list_frame = None
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
        self._search_entry.bind("<KeyRelease>", lambda _event: self._render_nodes())

        filter_bar = ctk.CTkFrame(toolbar, fg_color="transparent")
        filter_bar.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self._filter_combo = ctk.CTkComboBox(
            filter_bar,
            values=list(self.FILTER_OPTIONS),
            width=108,
            command=lambda _value: self._render_nodes(),
            **combo_style(),
        )
        self._filter_combo.set("全部")
        self._filter_combo.pack(side="left", padx=(0, 8))

        self._region_combo = ctk.CTkComboBox(
            filter_bar,
            values=[self.REGION_ALL],
            width=112,
            command=lambda _value: self._render_nodes(),
            **combo_style(),
        )
        self._region_combo.set(self.REGION_ALL)
        self._region_combo.pack(side="left", padx=(0, 8))

        self._quality_combo = ctk.CTkComboBox(
            filter_bar,
            values=list(self.QUALITY_OPTIONS),
            width=112,
            command=lambda _value: self._render_nodes(),
            **combo_style(),
        )
        self._quality_combo.set("全部质量")
        self._quality_combo.pack(side="left", padx=(0, 8))

        match_button = ctk.CTkButton(
            filter_bar,
            text="勾选匹配",
            width=74,
            command=lambda: self._set_matching_checked(True),
            **button_style("secondary", compact=True),
        )
        match_button.pack(side="left", padx=(0, 6))
        clear_button = ctk.CTkButton(
            filter_bar,
            text="清空勾选",
            width=74,
            command=lambda: self._set_matching_checked(False),
            **button_style("secondary", compact=True),
        )
        clear_button.pack(side="left")
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
            height=238,
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

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        state = "normal" if self._enabled else "disabled"
        for widget in (self._search_entry, self._filter_combo, self._region_combo, self._quality_combo, *self._batch_buttons):
            if widget:
                try:
                    widget.configure(state=state)
                except Exception:
                    pass
        self._render_nodes()

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
            return f"当前筛选 {len(self._filtered_nodes())} 个节点"
        return f"全部 {len(self._nodes)} 个节点"

    def select_by_key(self, node_key: str) -> bool:
        key = str(node_key or "")
        if not key:
            return False
        for item in self._nodes:
            if self._node_key(item) == key:
                self._selected_key = key
                self._render_nodes()
                return True
        return False

    def _render_nodes(self):
        if not self._list_frame:
            return
        for child in self._list_frame.winfo_children():
            child.destroy()

        matches = self._filtered_nodes()
        total = len(self._nodes)
        ok_count = sum(1 for item in self._nodes if remote_proxy.proxy_node_latency_ok(self._latency_for(item)))
        measured_count = sum(1 for item in self._nodes if self._latency_for(item) is not None)
        quality_count = sum(1 for item in self._nodes if remote_proxy.proxy_node_quality_measured(self._quality_for(item)))
        high_quality_count = sum(1 for item in self._nodes if remote_proxy.proxy_node_quality_for_ai_proxy_ok(self._quality_for(item)))
        checked_count = len(self._checked_keys)
        visible = matches[: self.MAX_VISIBLE_ROWS]
        selected_item = self.selected_item()
        if selected_item in matches and selected_item not in visible:
            visible = [selected_item] + visible[: max(0, self.MAX_VISIBLE_ROWS - 1)]

        suffix = ""
        if len(matches) > len(visible):
            suffix = f"；显示前 {len(visible)} 个，请继续搜索缩小范围"
        if self._summary_label:
            self._summary_label.configure(
                text=(
                    f"节点 {total} 个；可连 {ok_count}；延迟已测 {measured_count}；"
                    f"质量已测 {quality_count}；家宽高质 {high_quality_count}；"
                    f"批量勾选 {checked_count}；匹配 {len(matches)} 个{suffix}"
                )
            )

        if not visible:
            ctk.CTkLabel(
                self._list_frame,
                text=self._empty_message(total, quality_count),
                text_color=COLORS["muted"],
                font=font(12),
            ).pack(fill="x", padx=12, pady=16)
            return

        for region, group_items in self._group_visible_nodes(visible):
            self._render_group_header(region, group_items)
            for item in group_items:
                self._render_row(item)

    def _group_visible_nodes(self, items):
        groups = []
        current_region = None
        current_items = []
        for item in items:
            region = remote_proxy.proxy_node_region(item.node)
            if current_items and region != current_region:
                groups.append((current_region, current_items))
                current_items = []
            current_region = region
            current_items.append(item)
        if current_items:
            groups.append((current_region, current_items))
        return groups

    def _render_group_header(self, region: str, items):
        keys = [self._node_key(item) for item in items]
        checked = sum(1 for key in keys if key in self._checked_keys)
        ok_count = sum(1 for item in items if remote_proxy.proxy_node_latency_ok(self._latency_for(item)))
        high_count = sum(1 for item in items if remote_proxy.proxy_node_quality_for_ai_proxy_ok(self._quality_for(item)))
        header = ctk.CTkFrame(self._list_frame, fg_color=COLORS["surface_alt"], corner_radius=6)
        header.pack(fill="x", padx=6, pady=(8, 0))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text=f"{region or '其他'}  {len(items)} 个 · 可连 {ok_count} · 家宽高质 {high_count} · 已勾选 {checked}",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=(10, 8), pady=6)
        ctk.CTkButton(
            header,
            text="全选",
            width=48,
            state="normal" if self._enabled else "disabled",
            command=lambda group_keys=tuple(keys): self._set_group_checked(group_keys, True),
            **button_style("secondary", compact=True),
        ).grid(row=0, column=1, sticky="e", padx=(0, 6), pady=5)
        ctk.CTkButton(
            header,
            text="清空",
            width=48,
            state="normal" if self._enabled else "disabled",
            command=lambda group_keys=tuple(keys): self._set_group_checked(group_keys, False),
            **button_style("secondary", compact=True),
        ).grid(row=0, column=2, sticky="e", padx=(0, 8), pady=5)

    def _render_row(self, item):
        node_key = self._node_key(item)
        node = item.node
        selected = node_key == self._selected_key
        latency = self._latency_for(item)
        latency_label = remote_proxy.proxy_node_latency_label(latency)
        latency_detail = remote_proxy.proxy_node_latency_detail(latency)
        latency_color = self._latency_color(latency)
        quality = self._quality_for(item)
        quality_label = remote_proxy.proxy_node_quality_label(quality)
        quality_score = remote_proxy.proxy_node_quality_score(quality)
        quality_ip = remote_proxy.proxy_node_quality_ip(quality)
        region = remote_proxy.proxy_node_region(node)
        node_type = str(node.get("type") or "").upper()
        server = str(node.get("server") or "")
        port = str(node.get("port") or "")

        row = ctk.CTkFrame(
            self._list_frame,
            **card_frame_kwargs(COLORS["primary"] if selected else COLORS["border_soft"]),
        )
        row.pack(fill="x", padx=6, pady=(6, 0))
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
        ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(8, 4), pady=8)

        ctk.CTkButton(
            row,
            text="已选" if selected else "选择",
            width=50,
            state="normal" if self._enabled else "disabled",
            command=lambda key=node_key: self._select(key),
            **button_style("primary" if selected else "secondary", compact=True),
        ).grid(row=0, column=1, rowspan=2, sticky="w", padx=(4, 8), pady=8)

        title = str(node.get("name") or item.display_name())
        title_label = ctk.CTkLabel(
            row,
            text=title,
            text_color=COLORS["text"],
            font=font(12, "bold"),
            anchor="w",
            justify="left",
        )
        title_label.grid(row=0, column=2, sticky="ew", pady=(8, 0))
        bind_wraplength(row, title_label, padding=280, min_width=180, max_width=760)

        meta_parts = [f"【{region}】", node_type, f"{server}:{port}" if port else server]
        if remote_proxy.proxy_node_quality_measured(quality):
            quality_part = f"{quality_label} {quality_score}"
            if quality_ip:
                quality_part += f" {quality_ip}"
            source_label = remote_proxy.proxy_node_quality_source_label(quality)
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
        )
        meta_label.grid(row=1, column=2, sticky="ew", pady=(1, 8))
        bind_wraplength(row, meta_label, padding=280, min_width=180, max_width=760)

        ctk.CTkLabel(
            row,
            text=latency_label,
            text_color=latency_color,
            font=font(12, "bold"),
            width=64,
            anchor="e",
        ).grid(row=0, column=3, rowspan=2, sticky="e", padx=(8, 10))

        ctk.CTkLabel(
            row,
            text=quality_label if quality else "未测质量",
            text_color=self._quality_color(quality),
            font=font(11, "bold"),
            width=82,
            anchor="e",
        ).grid(row=0, column=4, rowspan=2, sticky="e", padx=(0, 10))

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
        self._render_nodes()

    def _set_group_checked(self, keys, checked: bool):
        if checked:
            self._checked_keys.update(keys)
        else:
            for key in keys:
                self._checked_keys.discard(key)
        self._render_nodes()

    def _set_matching_checked(self, checked: bool):
        keys = [self._node_key(item) for item in self._filtered_nodes()]
        if checked:
            self._checked_keys.update(keys)
        else:
            for key in keys:
                self._checked_keys.discard(key)
        self._render_nodes()

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
        matches = []
        for item in self._nodes:
            latency = self._latency_for(item)
            quality = self._quality_for(item)
            if region_filter and region_filter != self.REGION_ALL and remote_proxy.proxy_node_region(item.node) != region_filter:
                continue
            if mode == "可连" and not remote_proxy.proxy_node_latency_ok(latency):
                continue
            if mode == "不可连" and (latency is None or remote_proxy.proxy_node_latency_ok(latency)):
                continue
            if mode == "未测速" and latency is not None:
                continue
            if not self._quality_matches(quality_filter, quality):
                continue
            if query and query not in self._search_blob(item):
                continue
            matches.append(item)
        return matches

    def _empty_message(self, total: int, quality_count: int) -> str:
        if total <= 0:
            return "暂无节点，请先拉取订阅"
        mode = self._quality_combo.get() if self._quality_combo else "全部质量"
        if mode and mode != "全部质量" and quality_count <= 0:
            return "暂无质量结果，可先点击“测质选家宽”"
        return "没有匹配的节点"

    def _search_text(self) -> str:
        if not self._search_entry:
            return ""
        return self._search_entry.get().strip().casefold()

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
        node = item.node
        parts = [
            str(item.index),
            str(node.get("name") or ""),
            str(node.get("type") or ""),
            str(node.get("server") or ""),
            remote_proxy.proxy_node_region(node),
            remote_proxy.proxy_node_latency_label(self._latency_for(item)),
            remote_proxy.proxy_node_quality_label(self._quality_for(item)),
            remote_proxy.proxy_node_quality_ip_type(self._quality_for(item)),
            remote_proxy.proxy_node_quality_ip(self._quality_for(item)),
            remote_proxy.proxy_node_quality_detail(self._quality_for(item)),
        ]
        return " ".join(parts).casefold()

    def _latency_for(self, item):
        return self._latency_results.get(self._node_key(item))

    def _quality_for(self, item):
        return self._quality_results.get(self._node_key(item))

    def _node_key(self, item) -> str:
        return remote_proxy.proxy_node_key(item.node)

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
        if "代理" in label or "机房" in label:
            return COLORS["danger"]
        if remote_proxy.proxy_node_quality_score(result) >= 65:
            return COLORS["warning"]
        return COLORS["muted"]

    def _quality_matches(self, mode: str, result) -> bool:
        if not mode or mode == "全部质量":
            return True
        measured = remote_proxy.proxy_node_quality_measured(result)
        label = remote_proxy.proxy_node_quality_label(result)
        ip_type = remote_proxy.proxy_node_quality_ip_type(result)
        risk = remote_proxy.proxy_node_quality_risk_score(result)
        if mode == "未测质量":
            return not measured
        if not measured:
            return False
        label_text = label.casefold()
        ip_type_text = ip_type.casefold()
        if mode == "家宽高质":
            return remote_proxy.proxy_node_quality_for_ai_proxy_ok(result)
        if mode == "家宽/运营商":
            return any(marker in label_text or marker in ip_type_text for marker in ("家宽", "家庭", "住宅", "residential", "home", "broadband", "运营商/宽带"))
        if mode == "低风险":
            return remote_proxy.proxy_node_quality_score(result) >= 75 and (risk is None or risk <= 35)
        if mode == "机房/商宽":
            return any(marker in label_text or marker in ip_type_text for marker in ("机房", "idc", "hosting", "datacenter", "business", "商宽", "企业"))
        if mode == "代理风险":
            return remote_proxy.proxy_node_quality_score(result) <= 40 or any(
                marker in label_text or marker in ip_type_text for marker in ("代理", "vpn", "tor", "proxy", "匿名")
            )
        return True

    def _update_region_options(self):
        if not self._region_combo:
            return
        regions = {remote_proxy.proxy_node_region(item.node) for item in self._nodes}
        ordered = [region for region in remote_proxy.PROXY_REGION_ORDER if region in regions]
        ordered.extend(sorted(regions - set(ordered)))
        values = [self.REGION_ALL] + ordered
        current = self._region_combo.get() or self.REGION_ALL
        self._region_combo.configure(values=values)
        if current not in values:
            self._region_combo.set(self.REGION_ALL)
