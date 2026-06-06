import webbrowser

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
        self._enabled = True
        self._search_entry = None
        self._filter_combo = None
        self._region_combo = None
        self._quality_combo = None
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
        self._search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._search_entry.bind("<KeyRelease>", lambda _event: self._render_nodes())

        self._filter_combo = ctk.CTkComboBox(
            toolbar,
            values=list(self.FILTER_OPTIONS),
            width=108,
            command=lambda _value: self._render_nodes(),
            **combo_style(),
        )
        self._filter_combo.set("全部")
        self._filter_combo.grid(row=0, column=1, sticky="e")

        self._region_combo = ctk.CTkComboBox(
            toolbar,
            values=[self.REGION_ALL],
            width=112,
            command=lambda _value: self._render_nodes(),
            **combo_style(),
        )
        self._region_combo.set(self.REGION_ALL)
        self._region_combo.grid(row=0, column=2, sticky="e", padx=(8, 0))

        self._quality_combo = ctk.CTkComboBox(
            toolbar,
            values=list(self.QUALITY_OPTIONS),
            width=112,
            command=lambda _value: self._render_nodes(),
            **combo_style(),
        )
        self._quality_combo.set("全部质量")
        self._quality_combo.grid(row=0, column=3, sticky="e", padx=(8, 0))

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
        self._latency_results = latency_results or {}
        self._quality_results = quality_results or {}
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
        self._render_nodes()

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        state = "normal" if self._enabled else "disabled"
        for widget in (self._search_entry, self._filter_combo, self._region_combo, self._quality_combo):
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
        claude_count = sum(1 for item in self._nodes if remote_proxy.proxy_node_quality_for_claude_ok(self._quality_for(item)))
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
                    f"质量已测 {quality_count}；家宽高质 {claude_count}；匹配 {len(matches)} 个{suffix}"
                )
            )

        if not visible:
            ctk.CTkLabel(
                self._list_frame,
                text="没有匹配的节点",
                text_color=COLORS["muted"],
                font=font(12),
            ).pack(fill="x", padx=12, pady=16)
            return

        for item in visible:
            self._render_row(item)

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
        row.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            row,
            text="已选" if selected else "选择",
            width=56,
            state="normal" if self._enabled else "disabled",
            command=lambda key=node_key: self._select(key),
            **button_style("primary" if selected else "secondary", compact=True),
        ).grid(row=0, column=0, rowspan=2, sticky="w", padx=(8, 8), pady=8)

        title = str(node.get("name") or item.display_name())
        title_label = ctk.CTkLabel(
            row,
            text=title,
            text_color=COLORS["text"],
            font=font(12, "bold"),
            anchor="w",
            justify="left",
        )
        title_label.grid(row=0, column=1, sticky="ew", pady=(8, 0))
        bind_wraplength(row, title_label, padding=330, min_width=180, max_width=700)

        meta_parts = [f"【{region}】", node_type, f"{server}:{port}" if port else server]
        if remote_proxy.proxy_node_quality_measured(quality):
            quality_part = f"{quality_label} {quality_score}"
            if quality_ip:
                quality_part += f" {quality_ip}"
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
        meta_label.grid(row=1, column=1, sticky="ew", pady=(1, 8))
        bind_wraplength(row, meta_label, padding=330, min_width=180, max_width=700)

        ctk.CTkLabel(
            row,
            text=latency_label,
            text_color=latency_color,
            font=font(12, "bold"),
            width=76,
            anchor="e",
        ).grid(row=0, column=2, rowspan=2, sticky="e", padx=(8, 10))

        ctk.CTkLabel(
            row,
            text=quality_label if quality else "未测质量",
            text_color=self._quality_color(quality),
            font=font(11, "bold"),
            width=82,
            anchor="e",
        ).grid(row=0, column=3, rowspan=2, sticky="e", padx=(0, 10))

        ctk.CTkButton(
            row,
            text="Ping0质量",
            width=76,
            command=lambda current=node: self._open_ping0(current),
            **button_style("accent", compact=True),
        ).grid(row=0, column=4, rowspan=2, sticky="e", padx=(0, 8), pady=8)

    def _select(self, node_key: str):
        self._selected_key = node_key
        item = self.selected_item()
        self._render_nodes()
        if item and self._on_select:
            self._on_select(item)

    def _filtered_nodes(self):
        query = self._search_text()
        mode = self._filter_combo.get() if self._filter_combo else "全部"
        region_filter = self._region_combo.get() if self._region_combo else self.REGION_ALL
        quality_filter = self._quality_combo.get() if self._quality_combo else "全部质量"
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

    def _search_text(self) -> str:
        if not self._search_entry:
            return ""
        return self._search_entry.get().strip().casefold()

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
        if remote_proxy.proxy_node_quality_for_claude_ok(result):
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
        if mode == "家宽高质":
            return remote_proxy.proxy_node_quality_for_claude_ok(result)
        if mode == "家宽/运营商":
            return any(marker in label or marker in ip_type for marker in ("家宽", "家庭", "住宅", "运营商/宽带"))
        if mode == "低风险":
            return remote_proxy.proxy_node_quality_score(result) >= 75 and (risk is None or risk <= 35)
        if mode == "机房/商宽":
            return any(marker in label or marker in ip_type for marker in ("机房", "IDC", "商宽", "企业"))
        if mode == "代理风险":
            return remote_proxy.proxy_node_quality_score(result) <= 40 or any(
                marker in label or marker in ip_type for marker in ("代理", "VPN", "Tor", "匿名")
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

    def _open_ping0(self, node: dict):
        try:
            webbrowser.open(remote_proxy.ping0_detail_url_for_proxy_node(node))
        except Exception:
            return
