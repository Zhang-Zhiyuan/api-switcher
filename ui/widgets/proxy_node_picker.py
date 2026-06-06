import customtkinter as ctk

from core import remote_proxy
from ui.theme import COLORS, button_style, card_frame_kwargs, combo_style, font, input_style


class ProxyNodePicker(ctk.CTkFrame):
    """Searchable, scrollable picker for large proxy subscriptions."""

    FILTER_OPTIONS = ("全部", "可连", "不可连", "未测速")
    MAX_VISIBLE_ROWS = 120

    def __init__(self, master, on_select=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._on_select = on_select
        self._nodes = []
        self._latency_results = {}
        self._selected_key = ""
        self._enabled = True
        self._search_entry = None
        self._filter_combo = None
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

    def set_nodes(self, nodes, latency_results=None, selected_key: str = ""):
        self._nodes = list(nodes or [])
        self._latency_results = latency_results or {}
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
        for widget in (self._search_entry, self._filter_combo):
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
        visible = matches[: self.MAX_VISIBLE_ROWS]

        suffix = ""
        if len(matches) > len(visible):
            suffix = f"；显示前 {len(visible)} 个，请继续搜索缩小范围"
        if self._summary_label:
            self._summary_label.configure(
                text=f"节点 {total} 个；可连 {ok_count}；已测速 {measured_count}；匹配 {len(matches)} 个{suffix}"
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
        ctk.CTkLabel(
            row,
            text=title,
            text_color=COLORS["text"],
            font=font(12, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="ew", pady=(8, 0))

        meta_parts = [f"【{region}】", node_type, f"{server}:{port}" if port else server]
        if latency_detail:
            meta_parts.append(latency_detail)
        ctk.CTkLabel(
            row,
            text=" · ".join(part for part in meta_parts if part),
            text_color=COLORS["muted"],
            font=font(11),
            anchor="w",
        ).grid(row=1, column=1, sticky="ew", pady=(1, 8))

        ctk.CTkLabel(
            row,
            text=latency_label,
            text_color=latency_color,
            font=font(12, "bold"),
            width=76,
            anchor="e",
        ).grid(row=0, column=2, rowspan=2, sticky="e", padx=(8, 10))

    def _select(self, node_key: str):
        self._selected_key = node_key
        item = self.selected_item()
        self._render_nodes()
        if item and self._on_select:
            self._on_select(item)

    def _filtered_nodes(self):
        query = self._search_text()
        mode = self._filter_combo.get() if self._filter_combo else "全部"
        matches = []
        for item in self._nodes:
            latency = self._latency_for(item)
            if mode == "可连" and not remote_proxy.proxy_node_latency_ok(latency):
                continue
            if mode == "不可连" and (latency is None or remote_proxy.proxy_node_latency_ok(latency)):
                continue
            if mode == "未测速" and latency is not None:
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
        ]
        return " ".join(parts).casefold()

    def _latency_for(self, item):
        return self._latency_results.get(self._node_key(item))

    def _node_key(self, item) -> str:
        return remote_proxy.proxy_node_key(item.node)

    def _latency_color(self, result) -> str:
        if remote_proxy.proxy_node_latency_ok(result):
            return COLORS["success"]
        if result is None:
            return COLORS["muted_soft"]
        return COLORS["danger"]
