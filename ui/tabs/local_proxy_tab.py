import threading
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from core import local_proxy, network_diagnostic_settings, remote_proxy, startup_manager
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font, input_style, textbox_style
from ui.widgets.proxy_node_picker import ProxyNodePicker
from ui.widgets.toast import show_toast


class LocalProxyTab(ctk.CTkScrollableFrame):
    """Tab for managing the Windows local AI proxy."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._subscription_entry = None
        self._subscription_picker = None
        self._fetch_button = None
        self._use_node_button = None
        self._latency_button = None
        self._quality_button = None
        self._quality_settings_button = None
        self._ping0_button = None
        self._subscription_action_hint_label = None
        self._auto_refresh_var = ctk.BooleanVar(value=False)
        self._auto_refresh_check = None
        self._periodic_update_var = ctk.BooleanVar(value=False)
        self._periodic_update_check = None
        self._periodic_update_entry = None
        self._periodic_update_after_id = None
        self._periodic_update_running = False
        self._startup_refresh_after_id = None
        self._start_on_login_var = ctk.BooleanVar(value=False)
        self._keep_running_on_exit_var = ctk.BooleanVar(value=True)
        self._proxy_non_cn_var = ctk.BooleanVar(value=False)
        self._builtin_site_vars = {}
        self._custom_target_entry = None
        self._custom_target_frame = None
        self._routing_status_label = None
        self._apply_routing_button = None
        self._cache_label = None
        self._selected_label = None
        self._node_text = None
        self._load_file_button = None
        self._start_button = None
        self._inspect_button = None
        self._test_button = None
        self._stop_button = None
        self._status_label = None
        self._subscription_nodes = []
        self._subscription_options = {}
        self._latency_results = {}
        self._quality_results = {}
        self._prefer_quality_sort = False
        self._busy = False
        self._saved_subscription_loaded = False
        self._saved_subscription_load_generation = 0
        self._build_ui()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(
            header,
            text="Win11 本机代理",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        subtitle = ctk.CTkLabel(
            header,
            text="只托管当前 Windows 用户的系统代理、环境变量和 VS Code 本机设置；用于本机 Codex、Claude Code、ChatGPT、Gemini 访问。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(header, subtitle, padding=12, min_width=260, max_width=920)

        policy_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        policy_frame.pack(fill="x", padx=14, pady=(0, 12))
        policy = ctk.CTkFrame(policy_frame, fg_color="transparent")
        policy.pack(fill="x", padx=14, pady=14)
        policy.grid_columnconfigure(1, weight=1)
        policy.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            policy,
            text="运行策略与代理范围",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=4, sticky="ew")

        startup_box = ctk.CTkFrame(policy, fg_color="transparent")
        startup_box.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        startup_box.grid_columnconfigure(0, weight=1)
        startup_box.grid_columnconfigure(1, weight=1)
        startup_box.grid_columnconfigure(2, weight=1)
        ctk.CTkCheckBox(
            startup_box,
            text="开机自动启动本机代理",
            variable=self._start_on_login_var,
            command=self._on_start_on_login_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        ).grid(row=0, column=0, sticky="w", padx=(0, 16))
        ctk.CTkCheckBox(
            startup_box,
            text="退出程序后继续运行",
            variable=self._keep_running_on_exit_var,
            command=self._on_keep_running_on_exit_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        ).grid(row=0, column=1, sticky="w", padx=(0, 16))
        ctk.CTkCheckBox(
            startup_box,
            text="代理大陆境外 IP",
            variable=self._proxy_non_cn_var,
            command=self._on_proxy_non_cn_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        ).grid(row=0, column=2, sticky="w")
        self._apply_routing_button = ctk.CTkButton(
            startup_box,
            text="应用规则",
            width=92,
            command=self._apply_saved_routing,
            **button_style("secondary", compact=True),
        )
        self._apply_routing_button.grid(row=0, column=3, sticky="e")

        ctk.CTkLabel(
            policy,
            text="内置站点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=2, column=0, sticky="nw", pady=(12, 0))
        builtin_box = ctk.CTkFrame(policy, fg_color="transparent")
        builtin_box.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
        builtin_box.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self._builtin_site_vars = {}
        for index, site in enumerate(local_proxy.LOCAL_PROXY_BUILTIN_SITES):
            site_id = str(site["id"])
            var = ctk.BooleanVar(value=False)
            self._builtin_site_vars[site_id] = var
            ctk.CTkCheckBox(
                builtin_box,
                text=str(site["label"]),
                variable=var,
                command=lambda value=site_id: self._on_builtin_site_toggle(value),
                checkbox_width=16,
                checkbox_height=16,
                text_color=COLORS["text"],
                font=font(12),
            ).grid(row=index // 4, column=index % 4, sticky="w", padx=(0, 14), pady=(0, 8))

        ctk.CTkLabel(
            policy,
            text="自定义",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=3, column=0, sticky="w", pady=(6, 0))
        custom_box = ctk.CTkFrame(policy, fg_color="transparent")
        custom_box.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        custom_box.grid_columnconfigure(0, weight=1)
        self._custom_target_entry = ctk.CTkEntry(
            custom_box,
            placeholder_text="输入网址或 IP，例如 youtube.com、https://example.com、8.8.8.8、1.1.1.0/24",
            **input_style(),
        )
        self._custom_target_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            custom_box,
            text="新增",
            width=72,
            command=self._add_custom_target,
            **button_style("accent", compact=True),
        ).grid(row=0, column=1, sticky="e")

        self._custom_target_frame = ctk.CTkFrame(policy, fg_color="transparent")
        self._custom_target_frame.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))

        self._routing_status_label = ctk.CTkLabel(
            policy,
            text="默认只代理 AI 相关域名；勾选内置站点或新增自定义目标后，会写入本机 mihomo 规则。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._routing_status_label.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        bind_wraplength(policy, self._routing_status_label, padding=20)

        node_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        node_frame.pack(fill="x", padx=14, pady=(0, 12))
        controls = ctk.CTkFrame(node_frame, fg_color="transparent")
        controls.pack(fill="x", padx=14, pady=14)
        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            controls,
            text="1 订阅来源",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=4, sticky="ew")
        ctk.CTkLabel(
            controls,
            text="订阅链接",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._subscription_entry = ctk.CTkEntry(
            controls,
            placeholder_text="粘贴 Clash/mihomo 订阅链接；只保存在本机缓存",
            **input_style(),
        )
        self._subscription_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        sub_actions = ctk.CTkFrame(controls, fg_color="transparent")
        sub_actions.grid(row=1, column=3, sticky="e", pady=(8, 0))
        self._fetch_button = ctk.CTkButton(
            sub_actions,
            text="拉取订阅",
            width=86,
            command=self._fetch_subscription,
            **button_style("secondary", compact=True),
        )
        self._fetch_button.pack(side="left", padx=(0, 6))
        self._auto_refresh_check = ctk.CTkCheckBox(
            sub_actions,
            text="启动时刷新",
            width=84,
            checkbox_width=16,
            checkbox_height=16,
            variable=self._auto_refresh_var,
            command=self._on_auto_refresh_toggle,
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._auto_refresh_check.pack(side="left")
        self._periodic_update_check = ctk.CTkCheckBox(
            sub_actions,
            text="定时热更新",
            width=96,
            checkbox_width=16,
            checkbox_height=16,
            variable=self._periodic_update_var,
            command=self._on_periodic_update_toggle,
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._periodic_update_check.pack(side="left", padx=(8, 0))
        self._periodic_update_entry = ctk.CTkEntry(
            sub_actions,
            width=48,
            placeholder_text="60",
            **input_style(),
        )
        self._periodic_update_entry.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            sub_actions,
            text="分钟",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left", padx=(4, 0))
        self._cache_label = ctk.CTkLabel(
            controls,
            text="本机缓存: 未加载",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._cache_label.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        bind_wraplength(controls, self._cache_label, padding=20)

        ctk.CTkLabel(
            controls,
            text="2 节点选择",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        ).grid(row=3, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        ctk.CTkLabel(
            controls,
            text="订阅节点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self._subscription_picker = ProxyNodePicker(
            controls,
            on_select=lambda _item: self._use_selected_subscription_node(show_message=False),
            on_scope_change=self._refresh_subscription_action_hint,
        )
        self._subscription_picker.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        self._subscription_picker.set_enabled(False)
        node_actions = ctk.CTkFrame(controls, fg_color="transparent")
        node_actions.grid(row=4, column=3, sticky="e", pady=(8, 0))
        ctk.CTkLabel(
            node_actions,
            text="批量",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(0, 4))
        self._latency_button = ctk.CTkButton(
            node_actions,
            text="测速范围",
            width=104,
            command=self._measure_subscription_latencies,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._latency_button.pack(anchor="e", pady=(0, 6))
        self._quality_button = ctk.CTkButton(
            node_actions,
            text="质量+复核",
            width=104,
            command=self._measure_subscription_qualities,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._quality_button.pack(anchor="e", pady=(0, 6))
        ctk.CTkLabel(
            node_actions,
            text="当前节点",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(2, 4))
        self._use_node_button = ctk.CTkButton(
            node_actions,
            text="使用当前",
            width=104,
            command=self._use_selected_subscription_node,
            state="disabled",
            **button_style("accent", compact=True),
        )
        self._use_node_button.pack(anchor="e")
        self._ping0_button = ctk.CTkButton(
            node_actions,
            text="测当前",
            width=104,
            command=self._measure_selected_subscription_quality,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._ping0_button.pack(anchor="e", pady=(6, 0))
        ctk.CTkLabel(
            node_actions,
            text="设置",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(8, 4))
        self._quality_settings_button = ctk.CTkButton(
            node_actions,
            text="质量源",
            width=104,
            command=self._open_proxy_quality_dialog,
            **button_style("primary", compact=True),
        )
        self._quality_settings_button.pack(anchor="e")
        self._subscription_action_hint_label = ctk.CTkLabel(
            node_actions,
            text="范围: -\n源: -",
            text_color=COLORS["muted_soft"],
            font=font(11),
            width=112,
            anchor="e",
            justify="right",
            wraplength=112,
        )
        self._subscription_action_hint_label.pack(anchor="e", pady=(8, 0))
        self._selected_label = ctk.CTkLabel(
            controls,
            text="待启动节点: 未选择",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._selected_label.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        bind_wraplength(controls, self._selected_label, padding=20)

        ctk.CTkLabel(
            controls,
            text="3 启动本机代理",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        ).grid(row=6, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        ctk.CTkLabel(
            controls,
            text="待启动节点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=7, column=0, sticky="nw", pady=(8, 0))
        self._node_text = ctk.CTkTextbox(
            controls,
            height=96,
            **textbox_style(monospace=True),
        )
        self._node_text.grid(row=7, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))

        actions = ctk.CTkFrame(controls, fg_color="transparent")
        actions.grid(row=7, column=3, sticky="ne", pady=(8, 0))
        ctk.CTkLabel(
            actions,
            text="节点来源",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(0, 4))
        self._load_file_button = ctk.CTkButton(
            actions,
            text="导入文件",
            width=104,
            command=self._load_node_file,
            **button_style("secondary", compact=True),
        )
        self._load_file_button.pack(anchor="e", pady=(0, 10))
        ctk.CTkLabel(
            actions,
            text="本机运行",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(0, 4))
        self._start_button = ctk.CTkButton(
            actions,
            text="启动本机",
            width=104,
            command=self._start_local_proxy,
            **button_style("accent", compact=True),
        )
        self._start_button.pack(anchor="e", pady=(0, 6))
        self._inspect_button = ctk.CTkButton(
            actions,
            text="检查状态",
            width=104,
            command=self._inspect_local_proxy,
            **button_style("secondary", compact=True),
        )
        self._inspect_button.pack(anchor="e", pady=(0, 6))
        self._test_button = ctk.CTkButton(
            actions,
            text="测试连通",
            width=104,
            command=self._probe_local_proxy,
            **button_style("secondary", compact=True),
        )
        self._test_button.pack(anchor="e", pady=(0, 6))
        self._stop_button = ctk.CTkButton(
            actions,
            text="停止并恢复",
            width=104,
            command=self._stop_local_proxy,
            **button_style("danger", compact=True),
        )
        self._stop_button.pack(anchor="e")

        self._status_label = ctk.CTkLabel(
            controls,
            text="本页只影响 Windows 本机；默认从 17897 端口启动，端口占用时会自动顺延。停止会恢复本工具启动前保存的代理设置。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._status_label.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        bind_wraplength(controls, self._status_label, padding=20)
        self.after(20, self.refresh)

    def destroy(self):
        self._cancel_startup_refresh()
        self._cancel_periodic_update()
        super().destroy()

    def refresh(self):
        self._load_proxy_preferences_ui()
        self.after(30, self._load_saved_subscription_ui)

    def _set_status(self, message: str, severity: str = "info"):
        if not self._status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._status_label.configure(text=message, text_color=color)

    def _set_cache_status(self, message: str, severity: str = "info"):
        if not self._cache_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._cache_label.configure(text=message, text_color=color)

    def _set_selected_summary(self, message: str, severity: str = "info"):
        if not self._selected_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._selected_label.configure(text=message, text_color=color)

    def _set_routing_status(self, message: str, severity: str = "info"):
        if not self._routing_status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._routing_status_label.configure(text=message, text_color=color)

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self._fetch_button,
            self._latency_button,
            self._quality_button,
            self._use_node_button,
            self._quality_settings_button,
            self._ping0_button,
            self._load_file_button,
            self._start_button,
            self._inspect_button,
            self._test_button,
            self._stop_button,
            self._apply_routing_button,
        ):
            if not button:
                continue
            try:
                if button in (self._use_node_button, self._latency_button, self._quality_button, self._ping0_button) and not self._subscription_options:
                    button.configure(state="disabled")
                else:
                    button.configure(state=state)
            except Exception:
                pass
        if self._auto_refresh_check:
            try:
                self._auto_refresh_check.configure(state=state)
            except Exception:
                pass
        if self._periodic_update_check:
            try:
                self._periodic_update_check.configure(state=state)
            except Exception:
                pass
        if self._subscription_picker:
            try:
                self._subscription_picker.set_enabled((not busy) and bool(self._subscription_options))
            except Exception:
                pass

    def _load_proxy_preferences_ui(self):
        preferences = local_proxy.load_local_proxy_preferences()
        self._start_on_login_var.set(bool(preferences.get("start_on_login")))
        self._keep_running_on_exit_var.set(bool(preferences.get("keep_running_on_exit", True)))
        self._proxy_non_cn_var.set(bool(preferences.get("proxy_non_cn")))
        builtin_sites = preferences.get("builtin_sites") if isinstance(preferences.get("builtin_sites"), dict) else {}
        for site_id, var in self._builtin_site_vars.items():
            var.set(bool(builtin_sites.get(site_id)))
        self._render_custom_targets(preferences.get("custom_targets") or [])
        enabled_sites = sum(1 for enabled in builtin_sites.values() if enabled)
        enabled_custom = sum(1 for item in preferences.get("custom_targets") or [] if item.get("enabled", True))
        mode = "大陆境外 IP 走代理" if preferences.get("proxy_non_cn") else "仅规则命中的站点走代理"
        self._set_routing_status(
            f"当前规则: {mode}；内置站点 {enabled_sites} 个，自定义目标 {enabled_custom} 个。"
        )

    def _render_custom_targets(self, entries):
        if not self._custom_target_frame:
            return
        for child in self._custom_target_frame.winfo_children():
            child.destroy()
        clean_entries = [item for item in entries or [] if isinstance(item, dict)]
        if not clean_entries:
            ctk.CTkLabel(
                self._custom_target_frame,
                text="尚未添加自定义网址或 IP",
                text_color=COLORS["muted"],
                font=font(12),
                anchor="w",
            ).pack(anchor="w")
            return
        for entry in clean_entries:
            row = ctk.CTkFrame(self._custom_target_frame, fg_color="transparent")
            row.pack(fill="x", pady=(0, 6))
            row.grid_columnconfigure(1, weight=1)
            target_id = str(entry.get("id") or "")
            var = ctk.BooleanVar(value=bool(entry.get("enabled", True)))
            ctk.CTkCheckBox(
                row,
                text="",
                variable=var,
                command=lambda item_id=target_id, value_var=var: self._on_custom_target_toggle(item_id, value_var),
                width=28,
                checkbox_width=16,
                checkbox_height=16,
            ).grid(row=0, column=0, sticky="w")
            label = f"{entry.get('target') or entry.get('value')} · {'IP' if entry.get('kind') == 'ip-cidr' else '域名'}"
            ctk.CTkLabel(
                row,
                text=label,
                text_color=COLORS["text"],
                font=font(12),
                anchor="w",
            ).grid(row=0, column=1, sticky="ew", padx=(4, 8))
            ctk.CTkButton(
                row,
                text="删除",
                width=58,
                command=lambda item_id=target_id: self._remove_custom_target(item_id),
                **button_style("danger", compact=True),
            ).grid(row=0, column=2, sticky="e")

    def _on_start_on_login_toggle(self):
        enabled = bool(self._start_on_login_var.get())
        if not enabled:
            try:
                local_proxy.set_local_proxy_start_on_login(False)
            except Exception as e:
                message = f"关闭本机代理开机自启失败: {e}"
                self._load_proxy_preferences_ui()
                self._set_routing_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return
            self._set_routing_status("已关闭本机代理开机自启；应用本身的开机自启状态不会被自动改动。")
            return

        startup_node_summary = local_proxy.local_proxy_startup_node_summary()
        node_text = self._node_input()
        if node_text:
            try:
                startup_node_summary = local_proxy.set_local_proxy_startup_node(node_text)
            except Exception as e:
                self._start_on_login_var.set(False)
                message = f"当前待启动节点无法保存，开机自启未开启: {e}"
                self._set_routing_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return
        if not startup_node_summary:
            self._start_on_login_var.set(False)
            message = "请先选择或填入一个有效节点，再开启开机自动启动。"
            self._set_routing_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        previous_startup = startup_manager.get_startup_status()
        should_rollback_app_startup = previous_startup.supported and not previous_startup.enabled
        try:
            status = startup_manager.set_startup_enabled(True)
            local_proxy.set_local_proxy_start_on_login(True)
        except Exception as e:
            self._start_on_login_var.set(False)
            rollback_errors = []
            try:
                local_proxy.set_local_proxy_start_on_login(False)
            except Exception as rollback_error:
                rollback_errors.append(f"代理偏好回滚失败: {rollback_error}")
            if should_rollback_app_startup:
                try:
                    startup_manager.set_startup_enabled(False)
                except Exception as rollback_error:
                    rollback_errors.append(f"应用自启回滚失败: {rollback_error}")
            message = f"开启本机代理开机自启失败: {e}"
            if rollback_errors:
                message = f"{message}；" + "；".join(rollback_errors)
            self._load_proxy_preferences_ui()
            self._set_routing_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        suffix = "" if status.matches_expected else "；应用自启命令不是当前版本，已按系统记录继续"
        self._set_routing_status(
            f"已开启本机代理开机自启；程序会随 Windows 进入托盘，并在后台自动启动节点: {startup_node_summary}"
            f"{suffix}。",
            "success",
        )

    def _on_keep_running_on_exit_toggle(self):
        enabled = bool(self._keep_running_on_exit_var.get())
        try:
            local_proxy.set_local_proxy_keep_running_on_exit(enabled)
        except Exception as e:
            message = f"保存退出后代理运行策略失败: {e}"
            self._load_proxy_preferences_ui()
            self._set_routing_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        if enabled:
            self._set_routing_status("已设置为退出程序后继续保持 Win11 本机代理运行。", "success")
        else:
            self._set_routing_status("已设置为退出程序时停止 Win11 本机代理并恢复启动前代理设置。", "warning")

    def _on_proxy_non_cn_toggle(self):
        enabled = bool(self._proxy_non_cn_var.get())
        try:
            local_proxy.set_local_proxy_non_cn_mode(enabled)
        except Exception as e:
            message = f"保存大陆境外 IP 代理开关失败: {e}"
            self._load_proxy_preferences_ui()
            self._set_routing_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        self._load_proxy_preferences_ui()
        self._apply_saved_routing("已开启大陆境外 IP 走代理。" if enabled else "已关闭大陆境外 IP 走代理。")

    def _on_builtin_site_toggle(self, site_id: str):
        enabled = bool(self._builtin_site_vars.get(site_id).get()) if site_id in self._builtin_site_vars else False
        try:
            local_proxy.set_builtin_proxy_site_enabled(site_id, enabled)
        except Exception as e:
            message = f"保存内置站点开关失败: {e}"
            self._load_proxy_preferences_ui()
            self._set_routing_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        self._load_proxy_preferences_ui()
        self._apply_saved_routing("内置站点代理规则已保存。")

    def _add_custom_target(self):
        raw = self._custom_target_entry.get().strip() if self._custom_target_entry else ""
        try:
            entry = local_proxy.add_custom_proxy_target(raw)
        except Exception as e:
            message = f"新增自定义代理目标失败: {e}"
            self._load_proxy_preferences_ui()
            self._set_routing_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        if self._custom_target_entry:
            self._custom_target_entry.delete(0, "end")
        self._load_proxy_preferences_ui()
        self._apply_saved_routing(f"已新增自定义代理目标: {entry.get('target')}")

    def _remove_custom_target(self, target_id: str):
        try:
            removed = local_proxy.remove_custom_proxy_target(target_id)
        except Exception as e:
            message = f"删除自定义代理目标失败: {e}"
            self._load_proxy_preferences_ui()
            self._set_routing_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        self._load_proxy_preferences_ui()
        if not removed:
            self._set_routing_status("要删除的自定义代理目标不存在，已刷新列表。", "warning")
            return
        self._apply_saved_routing("自定义代理目标已删除。")

    def _on_custom_target_toggle(self, target_id: str, value_var):
        try:
            local_proxy.set_custom_proxy_target_enabled(target_id, bool(value_var.get()))
        except Exception as e:
            message = f"保存自定义代理目标开关失败: {e}"
            self._load_proxy_preferences_ui()
            self._set_routing_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        self._load_proxy_preferences_ui()
        self._apply_saved_routing("自定义代理目标开关已保存。")

    def _apply_saved_routing(self, prefix: str = "代理范围规则已保存。"):
        if self._busy:
            self._set_routing_status(f"{prefix} 当前有代理操作在运行，稍后可点“应用规则”。", "warning")
            return

        def worker():
            return f"{prefix} {local_proxy.apply_local_proxy_routing_to_running()}"

        self._run_local_task(
            "正在把 Win11 代理规则应用到运行中的本机代理...",
            worker,
            "应用 Win11 代理规则",
        )

    def _load_saved_subscription_ui(self):
        if self._saved_subscription_loaded:
            return
        self._saved_subscription_loaded = True
        self._saved_subscription_load_generation += 1
        generation = self._saved_subscription_load_generation
        state = remote_proxy.load_proxy_subscription_state()
        url = str(state.get("url") or "").strip()
        auto_refresh = remote_proxy.proxy_subscription_auto_refresh_enabled("local")
        periodic_update = bool(state.get("local_periodic_update_enabled"))
        interval_minutes = str(state.get("local_periodic_update_interval_minutes") or "60")
        self._auto_refresh_var.set(auto_refresh)
        self._periodic_update_var.set(periodic_update)

        if url and self._subscription_entry:
            self._subscription_entry.delete(0, "end")
            self._subscription_entry.insert(0, url)
        if self._periodic_update_entry:
            self._periodic_update_entry.delete(0, "end")
            self._periodic_update_entry.insert(0, interval_minutes)

        if not url:
            self._schedule_periodic_update(initial=True)
            return

        self._set_cache_status("本机缓存: 正在后台恢复订阅...", "info")
        self._set_status("正在后台恢复本机缓存订阅；页面可先操作。")

        def run():
            cached = remote_proxy.load_cached_proxy_subscription()
            payload = {
                "cached": cached,
                "latencies": remote_proxy.load_proxy_subscription_latencies() if cached and cached.nodes else {},
                "qualities": remote_proxy.load_proxy_subscription_qualities() if cached and cached.nodes else {},
            }

            def finish():
                if not self.winfo_exists() or generation != self._saved_subscription_load_generation:
                    return
                cached_result = payload["cached"]
                if cached_result and cached_result.nodes:
                    self._latency_results = payload["latencies"]
                    self._quality_results = payload["qualities"]
                    self._prefer_quality_sort = bool(self._quality_results)
                    self._set_subscription_nodes(cached_result.nodes)
                    self._select_subscription_node_by_key(str(state.get("selected_node_key") or ""))
                    self._use_selected_subscription_node(show_message=False, persist_selection=False)
                    self._set_cache_status(
                        f"本机缓存: {len(cached_result.nodes)} 个节点；上次拉取 {state.get('last_fetched_at') or '-'}",
                        "success",
                    )
                    self._set_status(
                        f"已加载本机缓存订阅: {len(cached_result.nodes)} 个节点；上次拉取 {state.get('last_fetched_at') or '-'}"
                    )
                else:
                    self._set_cache_status("本机缓存: 未找到可用节点", "warning")
                    self._set_status("已恢复订阅链接；尚未找到可用本机缓存，可手动拉取订阅。", "warning")

                if url and auto_refresh:
                    self._schedule_startup_refresh()
                self._schedule_periodic_update(initial=True)

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, name="local-proxy-cache-load", daemon=True).start()

    def _select_subscription_node_by_key(self, node_key: str) -> bool:
        if not node_key or not self._subscription_picker:
            return False
        return self._subscription_picker.select_by_key(node_key)

    def _on_auto_refresh_toggle(self):
        enabled = bool(self._auto_refresh_var.get())
        remote_proxy.set_proxy_subscription_auto_refresh(enabled, scope="local")
        if enabled:
            self._set_status("已开启 Win11 代理启动时刷新；下次打开本页会自动重新拉取订阅并保留可用缓存。", "success")
            if self._subscription_url_input():
                self._fetch_subscription(auto=True, show_message=False)
        else:
            self._cancel_startup_refresh()
            self._set_status("已关闭 Win11 代理启动时刷新。")

    def _schedule_startup_refresh(self):
        self._cancel_startup_refresh()
        self._startup_refresh_after_id = self.after(800, self._run_startup_refresh)

    def _run_startup_refresh(self):
        self._startup_refresh_after_id = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._subscription_url_input() and bool(self._auto_refresh_var.get()):
            self._fetch_subscription(auto=True, show_message=False)

    def _cancel_startup_refresh(self):
        if not self._startup_refresh_after_id:
            return
        try:
            self.after_cancel(self._startup_refresh_after_id)
        except Exception:
            pass
        self._startup_refresh_after_id = None

    def _periodic_update_interval_minutes(self) -> int:
        raw = self._periodic_update_entry.get().strip() if self._periodic_update_entry else ""
        try:
            value = int(raw or "60")
        except ValueError:
            value = 60
        value = min(max(value, 5), 1440)
        if self._periodic_update_entry and raw != str(value):
            self._periodic_update_entry.delete(0, "end")
            self._periodic_update_entry.insert(0, str(value))
        return value

    def _on_periodic_update_toggle(self):
        enabled = bool(self._periodic_update_var.get())
        interval = self._periodic_update_interval_minutes()
        remote_proxy.save_proxy_subscription_state(
            local_periodic_update_enabled=enabled,
            local_periodic_update_interval_minutes=interval,
        )
        if enabled:
            self._set_status(f"已开启 Win11 代理定时热更新；每 {interval} 分钟拉取订阅，运行中代理会尝试无重启切换。", "success")
        else:
            self._set_status("已关闭 Win11 代理定时热更新。")
        self._schedule_periodic_update(initial=not enabled)

    def _schedule_periodic_update(self, initial: bool = False):
        self._cancel_periodic_update()
        if not bool(self._periodic_update_var.get()):
            return
        interval_minutes = self._periodic_update_interval_minutes()
        delay_minutes = 1 if initial else interval_minutes
        remote_proxy.save_proxy_subscription_state(local_periodic_update_interval_minutes=interval_minutes)
        self._periodic_update_after_id = self.after(delay_minutes * 60 * 1000, self._run_periodic_update)

    def _cancel_periodic_update(self):
        if not self._periodic_update_after_id:
            return
        try:
            self.after_cancel(self._periodic_update_after_id)
        except Exception:
            pass
        self._periodic_update_after_id = None

    def _run_periodic_update(self):
        if not bool(self._periodic_update_var.get()):
            return
        if self._periodic_update_running or self._busy:
            self._schedule_periodic_update()
            return
        url = self._subscription_url_input()
        if not url:
            self._set_status("Win11 代理定时更新跳过：尚未设置订阅链接。", "warning")
            self._schedule_periodic_update()
            return
        self._periodic_update_running = True
        self._set_cache_status("本机缓存: 定时更新中...")

        def run():
            try:
                result = remote_proxy.fetch_proxy_subscription(url)
                apply_message = local_proxy.refresh_running_local_ai_proxy_from_subscription(result.nodes)
                payload = {"ok": True, "result": result, "apply": apply_message, "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "apply": "", "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._periodic_update_running = False
                if not payload["ok"]:
                    self._set_cache_status("本机缓存: 定时更新失败，继续使用已有节点", "warning")
                    self._set_status(f"Win11 代理定时更新失败: {payload['error']}", "warning")
                    self._schedule_periodic_update()
                    return
                result = payload["result"]
                self._latency_results = remote_proxy.load_proxy_subscription_latencies()
                self._set_subscription_nodes(result.nodes)
                self._set_cache_status(f"本机缓存: 定时更新已保存 {len(result.nodes)} 个节点", "success")
                severity = self._periodic_update_message_severity(payload["apply"])
                self._set_status(f"Win11 代理定时热更新完成；{payload['apply']}", severity)
                self._schedule_periodic_update()

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _periodic_update_message_severity(self, message: str) -> str:
        text = str(message or "")
        if any(marker in text for marker in ("失败", "未完全", "跳过", "不可用", "没有测到")):
            return "warning"
        return "success"

    def _subscription_url_input(self) -> str:
        if not self._subscription_entry:
            return ""
        return self._subscription_entry.get().strip()

    def _selected_subscription_node_key(self) -> str:
        if not self._subscription_picker:
            return ""
        return self._subscription_picker.selected_key()

    def _set_subscription_nodes(self, nodes, preserve_key: str = ""):
        current_key = preserve_key or self._selected_subscription_node_key()
        self._subscription_nodes = list(
            remote_proxy.sort_proxy_subscription_nodes(
                nodes or [],
                self._latency_results,
                self._quality_results,
                self._prefer_quality_sort,
            )
        )
        options = {}
        for item in self._subscription_nodes:
            options[remote_proxy.proxy_node_key(item.node)] = item
        self._subscription_options = options

        if not self._subscription_picker:
            return
        self._subscription_picker.set_nodes(
            self._subscription_nodes,
            self._latency_results,
            current_key,
            self._quality_results,
        )
        self._subscription_picker.set_enabled(bool(options) and not self._busy)
        if current_key:
            self._select_subscription_node_by_key(current_key)
        if self._use_node_button:
            self._use_node_button.configure(state="normal" if options and not self._busy else "disabled")
        if self._latency_button:
            self._latency_button.configure(state="normal" if options and not self._busy else "disabled")
        if self._quality_button:
            self._quality_button.configure(state="normal" if options and not self._busy else "disabled")
        if self._ping0_button:
            self._ping0_button.configure(state="normal" if options and not self._busy else "disabled")
        self._refresh_subscription_action_hint()

    def _refresh_subscription_action_hint(self):
        if not self._subscription_action_hint_label:
            return
        scope = self._subscription_picker.batch_scope_label() if self._subscription_picker else "-"
        source = remote_proxy.quality_source_label_from_settings()
        color = COLORS["warning"] if source == "未启用检测源" else COLORS["muted_soft"]
        self._subscription_action_hint_label.configure(text=f"范围: {scope}\n源: {source}", text_color=color)

    def _fetch_subscription(self, auto: bool = False, show_message: bool = True):
        if self._busy:
            if show_message:
                show_toast(self.winfo_toplevel(), "订阅正在拉取中，请稍等", is_error=True)
            return
        url = self._subscription_url_input()
        if not url:
            message = "请先粘贴订阅链接"
            self._set_status(message, "warning")
            if show_message:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        self._saved_subscription_load_generation += 1
        self._set_busy(True)
        self._set_cache_status("本机缓存: 正在刷新订阅..." if auto else "本机缓存: 正在拉取订阅...")
        self._set_status("正在自动刷新订阅..." if auto else "正在拉取订阅并解析节点...")

        def run():
            try:
                payload = {"ok": True, "result": remote_proxy.fetch_proxy_subscription(url), "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if not payload["ok"]:
                    if auto and self._subscription_options:
                        message = f"自动刷新失败，已保留本机缓存: {payload['error']}"
                        severity = "warning"
                        self._set_cache_status("本机缓存: 自动刷新失败，继续使用已有节点", "warning")
                    else:
                        message = f"订阅拉取失败: {payload['error']}"
                        severity = "error"
                        self._set_cache_status("本机缓存: 拉取失败", "error")
                    self._set_status(message, severity)
                    if show_message:
                        show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                result = payload["result"]
                state = remote_proxy.load_proxy_subscription_state()
                self._latency_results = remote_proxy.load_proxy_subscription_latencies()
                self._quality_results = remote_proxy.load_proxy_subscription_qualities()
                self._prefer_quality_sort = bool(self._quality_results)
                self._set_subscription_nodes(result.nodes)
                if not self._select_subscription_node_by_key(str(state.get("selected_node_key") or "")):
                    self._use_selected_subscription_node(show_message=False)
                else:
                    self._use_selected_subscription_node(show_message=False, persist_selection=False)
                self._set_cache_status(
                    f"本机缓存: 已保存 {len(result.nodes)} 个节点；刚刚拉取",
                    "success",
                )
                message = f"订阅已保存到本机缓存；识别到 {len(result.nodes)} 个节点，已填入当前选择。"
                self._set_status(message, "success")
                if show_message:
                    show_toast(self.winfo_toplevel(), message)

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _measure_subscription_latencies(self):
        if self._busy:
            show_toast(self.winfo_toplevel(), "本机代理操作正在进行中，请稍等", is_error=True)
            return
        if not self._subscription_nodes:
            message = "请先拉取订阅，再测速选择节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        scope_nodes = self._subscription_batch_nodes()
        scope_label = self._subscription_batch_scope_label()
        node_count = len(scope_nodes)
        if not scope_nodes:
            message = "当前节点分组没有可测速的节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        self._set_busy(True)
        self._set_status(f"正在测试 {scope_label} 的 TCP 延迟；完成后会自动选择该范围内最低延迟节点...")

        def run():
            try:
                results = remote_proxy.measure_proxy_node_latencies(
                    tuple(scope_nodes),
                    timeout=3.0,
                    attempts=2,
                    max_workers=20,
                )
                payload = {"ok": True, "result": results, "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if not payload["ok"]:
                    message = f"节点测速失败: {payload['error']}"
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                self._latency_results.update(payload["result"] or {})
                self._prefer_quality_sort = False
                save_error = ""
                try:
                    remote_proxy.save_proxy_subscription_latencies(self._latency_results)
                except Exception as exc:
                    save_error = str(exc)
                self._set_subscription_nodes(self._subscription_nodes)
                fastest = self._fastest_subscription_node(scope_nodes)
                ok_count = sum(
                    1
                    for item in scope_nodes
                    if remote_proxy.proxy_node_latency_ok(
                        self._latency_results.get(remote_proxy.proxy_node_key(item.node))
                    )
                )
                if not fastest:
                    message = f"测速完成: {scope_label} 中 {ok_count}/{node_count} 个节点可连；未找到可用节点。"
                    if save_error:
                        message += f" 测速结果缓存失败: {save_error}"
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                fastest_key = remote_proxy.proxy_node_key(fastest.node)
                self._select_subscription_node_by_key(fastest_key)
                self._use_selected_subscription_node(show_message=False)
                latency = remote_proxy.proxy_node_latency_label(self._latency_results.get(fastest_key))
                region = remote_proxy.proxy_node_region(fastest.node)
                message = f"测速完成: 基于 {scope_label}，{ok_count}/{node_count} 个节点可连；已选择最快节点【{region}】{latency}。"
                severity = "warning" if save_error else "success"
                if save_error:
                    message += f" 测速结果缓存失败: {save_error}"
                self._set_status(message, severity)
                show_toast(self.winfo_toplevel(), message, is_error=bool(save_error))

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _subscription_batch_nodes(self):
        if self._subscription_picker:
            return self._subscription_picker.batch_items()
        return list(self._subscription_nodes)

    def _subscription_batch_scope_label(self) -> str:
        if self._subscription_picker:
            return self._subscription_picker.batch_scope_label()
        return f"全部 {len(self._subscription_nodes)} 个节点"

    def _quality_candidate_nodes(self, nodes=None):
        base_nodes = list(nodes if nodes is not None else self._subscription_batch_nodes())
        candidates = []
        measured_any = False
        for item in base_nodes:
            result = self._latency_results.get(remote_proxy.proxy_node_key(item.node))
            if result is not None:
                measured_any = True
            if remote_proxy.proxy_node_latency_ok(result):
                candidates.append(item)
        return candidates if measured_any else base_nodes

    def _measure_subscription_qualities(self):
        if self._busy:
            show_toast(self.winfo_toplevel(), "本机代理操作正在进行中，请稍等", is_error=True)
            return
        if not self._subscription_nodes:
            message = "请先拉取订阅，再检测节点 IP 质量"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        scope_nodes = self._subscription_batch_nodes()
        scope_label = self._subscription_batch_scope_label()
        candidates = self._quality_candidate_nodes(scope_nodes)
        if not candidates:
            message = "当前测速结果里没有可连节点；请先重新测速，再检测 AI 代理高质量节点。"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        node_count = len(candidates)
        settings = network_diagnostic_settings.load_settings()
        services = settings.enabled_services()
        source_label = remote_proxy.quality_source_label_from_settings(settings, services)
        if not services:
            message = "请先在“质量检测源”启用至少一个检测源"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        self._set_busy(True)
        self._set_status(
            f"正在基于 {source_label} 检测 {scope_label} 中 {node_count} 个候选节点；"
            "运行中的本机代理会继续热更新候选并复核 OpenAI/Claude/Gemini..."
        )
        candidate_nodes = tuple(candidates)
        existing_quality_results = dict(self._quality_results)
        existing_latency_results = dict(self._latency_results)

        def run():
            try:
                results = remote_proxy.assess_proxy_node_qualities(
                    candidate_nodes,
                    timeout=5.0,
                    max_workers=8,
                    settings=settings,
                    enabled_services=services,
                )
                merged_results = dict(existing_quality_results)
                merged_results.update(results or {})
                best = remote_proxy.best_proxy_subscription_node_for_ai_proxy(
                    candidate_nodes,
                    merged_results,
                    existing_latency_results,
                )
                verify_message = ""
                verified_key = ""
                verify_error = ""
                try:
                    running = local_proxy.inspect_local_ai_proxy().running
                except Exception as exc:
                    running = False
                    verify_error = f"本机代理状态读取失败: {exc}"
                if best and running:
                    try:
                        verify_message = local_proxy.reload_local_ai_proxy_verified(
                            remote_proxy.format_proxy_node(best.node),
                            remote_proxy.ranked_proxy_subscription_nodes_for_ai_probe(
                                candidate_nodes,
                                merged_results,
                                existing_latency_results,
                            ),
                            quality_results=merged_results,
                        )
                        verified_key = local_proxy.current_local_ai_proxy_node_key()
                    except Exception as exc:
                        verify_error = str(exc)
                payload = {
                    "ok": True,
                    "result": results,
                    "merged_result": merged_results,
                    "best_key": remote_proxy.proxy_node_key(best.node) if best else "",
                    "verified_key": verified_key,
                    "verify_message": verify_message,
                    "verify_error": verify_error,
                    "verify_running": bool(best and running),
                    "error": None,
                }
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if not payload["ok"]:
                    message = f"节点 IP 质量检测失败: {payload['error']}"
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                self._quality_results.update(payload["result"] or {})
                self._prefer_quality_sort = True
                save_error = ""
                try:
                    remote_proxy.save_proxy_subscription_qualities(self._quality_results)
                except Exception as exc:
                    save_error = str(exc)

                self._set_subscription_nodes(self._subscription_nodes)
                tested_count = sum(
                    1
                    for item in candidate_nodes
                    if remote_proxy.proxy_node_quality_measured(
                        self._quality_results.get(remote_proxy.proxy_node_key(item.node))
                    )
                )
                high_count = sum(
                    1
                    for item in candidate_nodes
                    if remote_proxy.proxy_node_quality_for_ai_proxy_ok(
                        self._quality_results.get(remote_proxy.proxy_node_key(item.node))
                    )
                )
                best_key = str(payload.get("verified_key") or payload.get("best_key") or "")
                if best_key and not self._select_subscription_node_by_key(best_key):
                    best_key = str(payload.get("best_key") or "")
                if not best_key or not self._select_subscription_node_by_key(best_key):
                    message = f"质量检测完成: 本次 {len(payload['result'] or {})} 个；暂无可用质量结果。"
                    if save_error:
                        message += f" 质量结果缓存失败: {save_error}"
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                self._use_selected_subscription_node(show_message=False)
                selected = self._subscription_picker.selected_item() if self._subscription_picker else None
                selected_node = selected.node if selected else {}
                quality = self._quality_results.get(best_key)
                region = remote_proxy.proxy_node_region(selected_node)
                label = remote_proxy.proxy_node_quality_label(quality)
                score = remote_proxy.proxy_node_quality_score(quality)
                basis = remote_proxy.proxy_node_quality_source_label(quality)
                verify_message = str(payload.get("verify_message") or "")
                verify_error = str(payload.get("verify_error") or "")
                verify_running = bool(payload.get("verify_running"))
                verify_ok = remote_proxy._probe_summary_all_ok(verify_message)
                severity = (
                    "success"
                    if remote_proxy.proxy_node_quality_for_ai_proxy_ok(quality)
                    and not save_error
                    and verify_running
                    and verify_ok
                    and not verify_error
                    else "warning"
                )
                message = (
                    f"质量检测完成: 基于 {basis}，{scope_label} 家宽高质 {high_count}/{tested_count}；"
                    f"已选择【{region}】{label} 评分{score}。"
                )
                if verify_message:
                    message += f" AI 复核: {remote_proxy._compact_probe_summary(verify_message)}"
                elif verify_error:
                    message += f" AI 复核跳过: {verify_error}"
                else:
                    message += " 本机代理未运行，启动或热更新时会继续做 AI 连通性验证。"
                if save_error:
                    message += f" 质量结果缓存失败: {save_error}"
                self._set_status(message, severity)
                show_toast(self.winfo_toplevel(), message, is_error=bool(save_error or verify_error))

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _open_proxy_quality_dialog(self):
        top = self.winfo_toplevel()
        if hasattr(top, "_show_proxy_quality_dialog"):
            dialog = top._show_proxy_quality_dialog()
            if dialog is not None:
                self._refresh_subscription_action_hint()
                self._set_status("已打开代理质量检测；可配置检测源和 API Key 池。")
                show_toast(top, "已打开代理质量检测，可选择 Ping0 / ProxyCheck / ipapi.is / VPNAPI")
            else:
                self._set_status("代理质量检测窗口打开失败。", "error")
                show_toast(top, "代理质量检测窗口打开失败", is_error=True)
            return
        self._set_status("无法打开代理质量检测窗口。", "error")
        show_toast(top, "无法打开代理质量检测窗口", is_error=True)

    def _measure_selected_subscription_quality(self):
        if not self._subscription_picker:
            return
        item = self._subscription_picker.selected_item()
        if not item:
            message = "请先拉取订阅并选择一个节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        settings = network_diagnostic_settings.load_settings()
        services = settings.enabled_services()
        source_label = remote_proxy.quality_source_label_from_settings(settings, services)
        if not services:
            message = "请先在“质量检测源”启用至少一个检测源"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        node_summary = remote_proxy.describe_proxy_node(item.node)
        self._set_busy(True)
        self._set_status(f"正在基于 {source_label} 检测选中节点: {node_summary}")

        def run():
            try:
                result = remote_proxy.assess_proxy_node_quality(
                    item.node,
                    timeout=5.0,
                    settings=settings,
                    enabled_services=services,
                )
                payload = {"ok": True, "result": result, "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if not payload["ok"]:
                    message = f"选中节点质量检测失败: {payload['error']}"
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return
                result = payload["result"]
                node_key = remote_proxy.proxy_node_key(item.node)
                self._quality_results[node_key] = result
                self._prefer_quality_sort = True
                save_error = ""
                try:
                    remote_proxy.save_proxy_subscription_qualities(self._quality_results)
                except Exception as exc:
                    save_error = str(exc)
                self._set_subscription_nodes(self._subscription_nodes, preserve_key=node_key)
                label = remote_proxy.proxy_node_quality_label(result)
                score = remote_proxy.proxy_node_quality_score(result)
                basis = remote_proxy.proxy_node_quality_source_label(result)
                detail = remote_proxy.proxy_node_quality_detail(result)
                message = f"选中节点检测完成: 基于 {basis}，{label} 评分{score}"
                if detail:
                    message += f"；{detail}"
                if save_error:
                    message += f" 质量结果缓存失败: {save_error}"
                severity = "success" if result.ok and not save_error else "warning"
                self._set_status(message, severity)
                show_toast(self.winfo_toplevel(), message, is_error=bool(save_error))

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _fastest_subscription_node(self, nodes=None):
        fastest = None
        fastest_latency = None
        for item in (nodes if nodes is not None else self._subscription_nodes):
            result = self._latency_results.get(remote_proxy.proxy_node_key(item.node))
            latency = remote_proxy.proxy_node_latency_ms(result)
            if latency is None or not remote_proxy.proxy_node_latency_ok(result):
                continue
            if fastest is None or latency < fastest_latency:
                fastest = item
                fastest_latency = latency
        return fastest

    def _use_selected_subscription_node(self, show_message: bool = True, persist_selection: bool = True):
        if not self._subscription_picker:
            return
        item = self._subscription_picker.selected_item()
        if not item:
            message = "请先拉取订阅并选择一个节点"
            self._set_status(message, "warning")
            if show_message:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        node_text = remote_proxy.format_proxy_node(item.node)
        if self._node_text:
            self._node_text.delete("1.0", "end")
            self._node_text.insert("1.0", node_text)
        if persist_selection:
            remote_proxy.set_proxy_subscription_selected_node(item.node)
        node_summary = remote_proxy.describe_proxy_node(item.node)
        self._set_selected_summary(f"待启动节点: {node_summary}", "success")
        message = f"已填入待启动节点: {node_summary}"
        self._set_status(message, "success")
        if show_message:
            show_toast(self.winfo_toplevel(), message)

    def _node_input(self) -> str:
        if not self._node_text:
            return ""
        return self._node_text.get("1.0", "end").strip()

    def _load_node_file(self):
        path = filedialog.askopenfilename(
            title="选择 Clash 节点文件",
            filetypes=[
                ("配置文件", "*.yaml *.yml *.txt *.json"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"读取代理文件失败: {e}", is_error=True)
            return
        if self._node_text:
            self._node_text.delete("1.0", "end")
            self._node_text.insert("1.0", content.strip())
        try:
            node_summary = remote_proxy.describe_proxy_node(remote_proxy.parse_proxy_node(content))
            self._set_selected_summary(f"待启动节点: {node_summary}", "success")
            self._set_status(f"已载入代理文件: {Path(path).name}；将使用节点 {node_summary}", "success")
        except Exception as e:
            self._set_selected_summary("待启动节点: 文件内容暂未识别", "warning")
            self._set_status(f"已载入代理文件: {Path(path).name}；暂未识别到可用节点: {e}", "warning")

    def _run_local_task(self, busy_message: str, worker, success_prefix: str, on_success=None, severity_from_result=None):
        if self._busy:
            show_toast(self.winfo_toplevel(), "本机代理操作正在进行中，请稍等", is_error=True)
            return
        self._set_busy(True)
        self._set_status(busy_message)

        def run():
            try:
                payload = {"ok": True, "result": worker(), "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if not payload["ok"]:
                    message = f"{success_prefix}失败: {payload['error']}"
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return
                if on_success:
                    try:
                        on_success(payload["result"])
                    except Exception:
                        pass
                message = str(payload["result"])
                severity = severity_from_result(message) if severity_from_result else "success"
                self._set_status(message, severity)
                show_toast(self.winfo_toplevel(), message, is_error=severity == "warning")

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _start_local_proxy(self):
        proxy_text = self._node_input()
        try:
            proxy_node = remote_proxy.parse_proxy_node(proxy_text)
            node_summary = remote_proxy.describe_proxy_node(proxy_node)
            self._set_selected_summary(f"待启动节点: {node_summary}", "success")
        except Exception as e:
            message = f"代理节点格式不正确: {e}"
            self._set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        def do_start():
            def sync_started_node(_result):
                key = local_proxy.current_local_ai_proxy_node_key()
                if key and self._select_subscription_node_by_key(key):
                    self._use_selected_subscription_node(show_message=False)

            self._run_local_task(
                "正在启动 Windows 本机 AI 代理，并验证 OpenAI/Claude/Gemini 连通性...",
                lambda: local_proxy.install_local_ai_proxy_verified(
                    proxy_text,
                    tuple(self._subscription_nodes),
                    quality_results=self._quality_results,
                ),
                "启动本机 AI 代理",
                on_success=sync_started_node,
                severity_from_result=lambda message: "warning"
                if "验证未完全通过" in message or "自动尝试" in message
                else "success",
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="启动 Win11 本机代理",
            message=(
                "将使用当前节点启动 Windows 本机 mihomo，并写入当前 Windows 用户的 "
                "HTTP_PROXY/HTTPS_PROXY/ALL_PROXY、VS Code 本机代理设置，以及 Win11 当前用户系统代理。\n"
                f"识别到节点: {node_summary}\n"
                "mihomo 会按上方“代理范围”规则转发：AI 站点始终走代理，内置站点/自定义目标和境外 IP 模式按开关生效。"
            ),
            on_confirm=do_start,
        )

    def _inspect_local_proxy(self):
        self._run_local_task(
            "正在检查 Windows 本机 AI 代理状态...",
            lambda: local_proxy.inspect_local_ai_proxy().summary(),
            "检查本机 AI 代理",
        )

    def _probe_local_proxy(self):
        self._run_local_task(
            "正在通过本机 AI 代理测试 OpenAI/Claude/Gemini 连通性...",
            local_proxy.probe_local_ai_proxy,
            "测试本机 AI 代理",
        )

    def _stop_local_proxy(self):
        def do_stop():
            self._run_local_task(
                "正在停止 Windows 本机 AI 代理并恢复本工具写入的代理环境...",
                local_proxy.stop_local_ai_proxy,
                "停止本机 AI 代理",
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="停止 Win11 本机代理",
            message="将停止本工具启动的本机 mihomo，并尽量恢复启动前的 Windows 用户代理环境变量、Win11 系统代理和 VS Code 代理设置。",
            on_confirm=do_stop,
        )
