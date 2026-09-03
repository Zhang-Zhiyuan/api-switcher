import threading
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk

from core.lazy_imports import LazyModule
from core.local_proxy_constants import (
    LOCAL_PROXY_AI_SERVICES,
    LOCAL_PROXY_BUILTIN_SITES,
    LOCAL_PROXY_CUSTOM_ROUTE_ID,
)
from ui.dialogs.confirm_dialog import ConfirmDialog
from ui.feedback import infer_feedback_severity, safe_feedback_text
from ui.tabs.tab_visibility import is_active_tab
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font, input_style, recent_user_scroll, textbox_style
from ui.widgets.proxy_node_picker import ProxyNodePicker
from ui.widgets.toast import show_toast


local_proxy = LazyModule("core.local_proxy")
network_diagnostic_settings = LazyModule("core.network_diagnostic_settings")
remote_proxy = LazyModule("core.remote_proxy")
startup_manager = LazyModule("core.startup_manager")

LOCAL_YAML_NODE_ONLY_NOTICE = "本地 YAML 只导入 proxies 节点，不继承顶层 dns/tun。"
SERVICE_ROUTE_DEFAULT_LABEL = "跟随当前节点"
SERVICE_ROUTE_MISSING_LABEL = "原订阅已删除（请重新选择）"


def _local_proxy_tab_layout(width: int) -> tuple[bool, int, int, int, bool]:
    """Return outer stacking and inner column counts for the proxy form."""

    available = max(1, int(width))
    stacked = available < 760
    return (
        stacked,
        2 if stacked else 4,
        2 if available < 620 else 4,
        2 if available < 620 else 4,
        available < 520,
    )


class LocalProxyTab(ctk.CTkScrollableFrame):
    """Tab for managing the Windows local AI proxy."""

    STARTUP_REFRESH_DELAY_MS = 2500
    SCROLL_IDLE_BUILD_MS = 850
    SCROLL_RETRY_BUILD_MS = 260

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._destroyed = False
        self._ui_dispatch = self._resolve_ui_dispatch()
        self._subscription_entry = None
        self._subscription_profile_combo = None
        self._subscription_name_entry = None
        self._subscription_profile_save_button = None
        self._subscription_profile_delete_button = None
        self._subscription_profile_options = {}
        self._subscription_profiles_snapshot = []
        self._subscription_profile_loading = False
        self._subscription_picker = None
        self._subscription_picker_host = None
        self._subscription_picker_after_id = None
        self._deferred_subscription_picker_pending = False
        self._fetch_button = None
        self._import_subscription_file_button = None
        self._manual_hot_update_button = None
        self._use_node_button = None
        self._hot_update_node_button = None
        self._latency_button = None
        self._quality_button = None
        self._quality_cancel_button = None
        self._quality_cancel_event = None
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
        self._subscription_hot_update_lock_owned = False
        self._initial_refresh_after_id = None
        self._saved_subscription_after_id = None
        self._startup_refresh_after_id = None
        self._start_on_login_var = ctk.BooleanVar(value=False)
        self._keep_running_on_exit_var = ctk.BooleanVar(value=True)
        self._wsl_share_var = ctk.BooleanVar(value=False)
        self._wsl_share_check = None
        self._wsl_optimize_button = None
        self._proxy_non_cn_var = ctk.BooleanVar(value=False)
        self._strict_privacy_var = ctk.BooleanVar(value=False)
        self._strict_privacy_check = None
        self._builtin_site_vars = {}
        self._builtin_site_tiles = []
        self._service_route_combos = {}
        self._service_route_bindings = {}
        self._service_route_profile_options = {SERVICE_ROUTE_DEFAULT_LABEL: ""}
        self._service_route_profile_labels = {"": SERVICE_ROUTE_DEFAULT_LABEL}
        self._service_route_combo_loading = False
        self._custom_target_entry = None
        self._custom_target_frame = None
        self._routing_status_label = None
        self._apply_routing_button = None
        self._cache_label = None
        self._selected_label = None
        self._node_text = None
        self._node_text_host = None
        self._node_text_after_id = None
        self._deferred_node_text_pending = False
        self._deferred_initial_refresh_pending = False
        self._deferred_saved_subscription_pending = False
        self._load_file_button = None
        self._start_button = None
        self._inspect_button = None
        self._core_update_button = None
        self._cleanup_proxy_button = None
        self._test_button = None
        self._wsl_test_button = None
        self._stop_button = None
        self._status_label = None
        self._subscription_nodes = []
        self._subscription_options = {}
        self._latency_results = {}
        self._quality_results = {}
        self._prefer_quality_sort = False
        self._busy = False
        self._responsive_after_id = None
        self._responsive_state = None
        self._saved_subscription_loaded = False
        self._saved_subscription_load_generation = 0
        self._preferences_load_generation = 0
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
            text=(
                "托管当前 Windows 用户的系统代理、环境变量和 VS Code 本机设置；"
                "可安全共享给默认 WSL 发行版中的 Codex/Claude Code。"
                "这是应用层代理，不是 VPN/TUN。"
            ),
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
        self._policy_grid = policy
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
        self._startup_box = startup_box
        startup_box.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        startup_box.grid_columnconfigure(0, weight=1)
        startup_box.grid_columnconfigure(1, weight=1)
        startup_box.grid_columnconfigure(2, weight=1)
        start_on_login_check = ctk.CTkCheckBox(
            startup_box,
            text="开机自动启动本机代理",
            variable=self._start_on_login_var,
            command=self._on_start_on_login_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        )
        start_on_login_check.grid(row=0, column=0, sticky="w", padx=(0, 16))
        keep_running_check = ctk.CTkCheckBox(
            startup_box,
            text="退出程序后继续运行",
            variable=self._keep_running_on_exit_var,
            command=self._on_keep_running_on_exit_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        )
        keep_running_check.grid(row=0, column=1, sticky="w", padx=(0, 16))
        proxy_non_cn_check = ctk.CTkCheckBox(
            startup_box,
            text="代理大陆境外 IP",
            variable=self._proxy_non_cn_var,
            command=self._on_proxy_non_cn_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        )
        proxy_non_cn_check.grid(row=0, column=2, sticky="w")
        self._wsl_share_check = ctk.CTkCheckBox(
            startup_box,
            text="共享到 WSL（Codex/Claude）",
            variable=self._wsl_share_var,
            command=self._on_wsl_share_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        )
        self._wsl_share_check.grid(row=0, column=3, sticky="w")
        self._wsl_optimize_button = ctk.CTkButton(
            startup_box,
            text="优化 WSL 网络",
            width=108,
            command=self._optimize_wsl_network,
            **button_style("secondary", compact=True),
        )
        self._wsl_optimize_button.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._apply_routing_button = ctk.CTkButton(
            startup_box,
            text="应用规则",
            width=92,
            command=self._apply_saved_routing,
            **button_style("secondary", compact=True),
        )
        self._apply_routing_button.grid(row=1, column=3, sticky="e", pady=(6, 0))
        self._startup_items = [
            start_on_login_check,
            keep_running_check,
            proxy_non_cn_check,
            self._wsl_share_check,
            self._wsl_optimize_button,
            self._apply_routing_button,
        ]

        privacy_box = ctk.CTkFrame(policy, fg_color="transparent")
        self._privacy_box = privacy_box
        privacy_box.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self._strict_privacy_check = ctk.CTkCheckBox(
            privacy_box,
            text="严格隐私（应用层：进入 mihomo 的公网流量全部走代理）",
            variable=self._strict_privacy_var,
            command=self._on_strict_privacy_toggle,
            checkbox_width=18,
            checkbox_height=18,
            text_color=COLORS["text"],
            font=font(12),
        )
        self._strict_privacy_check.pack(anchor="w")
        privacy_notice = ctk.CTkLabel(
            privacy_box,
            text=(
                "不是 VPN/TUN：无法阻止忽略代理的程序、WebRTC/UDP 或系统 DNS/IPv6 绕过。"
                "严格模式只保证已进入 mihomo 的公网流量不 DIRECT，并对代理内 DNS/IPv6 "
                "使用更保守的配置。切换后请重启 Codex、Claude Code、VS Code 并打开新终端。"
            ),
            text_color=COLORS["warning"],
            font=font(11),
            anchor="w",
            justify="left",
        )
        privacy_notice.pack(anchor="w", fill="x", pady=(5, 0))
        bind_wraplength(privacy_box, privacy_notice, padding=8, min_width=240, max_width=920)

        self._service_routes_label = ctk.CTkLabel(
            policy,
            text="AI 服务线路",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._service_routes_label.grid(row=3, column=0, sticky="nw", pady=(12, 0))
        ai_route_box = ctk.CTkFrame(policy, fg_color="transparent")
        self._ai_route_box = ai_route_box
        ai_route_box.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
        self._ai_route_tiles = []
        for index, service in enumerate(LOCAL_PROXY_AI_SERVICES):
            service_id = str(service["id"])
            tile = ctk.CTkFrame(ai_route_box, fg_color="transparent")
            tile.grid(row=0, column=index, sticky="ew", padx=(0, 10) if index < 2 else 0)
            ctk.CTkLabel(
                tile,
                text=str(service["label"]),
                text_color=COLORS["text"],
                font=font(11),
                anchor="w",
            ).pack(anchor="w")
            combo = ctk.CTkComboBox(
                tile,
                values=[SERVICE_ROUTE_DEFAULT_LABEL],
                command=lambda label, value=service_id: self._on_service_route_selected(value, label),
                **combo_style(),
            )
            combo.pack(fill="x", pady=(3, 0))
            combo.set(SERVICE_ROUTE_DEFAULT_LABEL)
            self._service_route_combos[service_id] = combo
            self._ai_route_tiles.append(tile)
        route_notice = ctk.CTkLabel(
            ai_route_box,
            text=(
                "可把每项绑定到任一已保存订阅；绑定只读取该订阅已选节点和备用池，"
                "不会切换订阅页面，也不会改动 Codex/Claude 登录状态。"
                "第三方 API 地址请先加入下方自定义目标，再为自定义目标选择线路。"
            ),
            text_color=COLORS["muted_soft"],
            font=font(11),
            anchor="w",
            justify="left",
        )
        self._service_route_notice = route_notice
        route_notice.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        bind_wraplength(ai_route_box, route_notice, padding=8, min_width=220, max_width=850)

        self._builtin_sites_label = ctk.CTkLabel(
            policy,
            text="内置站点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._builtin_sites_label.grid(row=4, column=0, sticky="nw", pady=(12, 0))
        builtin_box = ctk.CTkFrame(policy, fg_color="transparent")
        self._builtin_box = builtin_box
        builtin_box.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
        builtin_box.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self._builtin_site_vars = {}
        self._builtin_site_checks = []
        self._builtin_site_tiles = []
        for index, site in enumerate(LOCAL_PROXY_BUILTIN_SITES):
            site_id = str(site["id"])
            var = ctk.BooleanVar(value=False)
            self._builtin_site_vars[site_id] = var
            tile = ctk.CTkFrame(builtin_box, fg_color="transparent")
            tile.grid(row=index // 4, column=index % 4, sticky="ew", padx=(0, 12), pady=(0, 8))
            checkbox = ctk.CTkCheckBox(
                tile,
                text=str(site["label"]),
                variable=var,
                command=lambda value=site_id: self._on_builtin_site_toggle(value),
                checkbox_width=16,
                checkbox_height=16,
                text_color=COLORS["text"],
                font=font(12),
            )
            checkbox.pack(anchor="w")
            combo = ctk.CTkComboBox(
                tile,
                values=[SERVICE_ROUTE_DEFAULT_LABEL],
                command=lambda label, value=site_id: self._on_service_route_selected(value, label),
                **combo_style(),
            )
            combo.pack(fill="x", pady=(4, 0))
            combo.set(SERVICE_ROUTE_DEFAULT_LABEL)
            self._service_route_combos[site_id] = combo
            self._builtin_site_checks.append(checkbox)
            self._builtin_site_tiles.append(tile)

        self._custom_target_label = ctk.CTkLabel(
            policy,
            text="自定义",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._custom_target_label.grid(row=5, column=0, sticky="w", pady=(6, 0))
        custom_box = ctk.CTkFrame(policy, fg_color="transparent")
        self._custom_box = custom_box
        custom_box.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        custom_box.grid_columnconfigure(0, weight=1)
        self._custom_target_entry = ctk.CTkEntry(
            custom_box,
            placeholder_text="输入网址或 IP，例如 youtube.com、https://example.com、8.8.8.8、1.1.1.0/24",
            **input_style(),
        )
        self._custom_target_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._custom_add_button = ctk.CTkButton(
            custom_box,
            text="新增",
            width=72,
            command=self._add_custom_target,
            **button_style("accent", compact=True),
        )
        self._custom_add_button.grid(row=0, column=1, sticky="e")

        self._custom_route_row = ctk.CTkFrame(custom_box, fg_color="transparent")
        self._custom_route_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ctk.CTkLabel(
            self._custom_route_row,
            text="自定义目标线路",
            text_color=COLORS["muted_soft"],
            font=font(11),
            anchor="w",
        ).pack(side="left", padx=(0, 8))
        custom_route_combo = ctk.CTkComboBox(
            self._custom_route_row,
            values=[SERVICE_ROUTE_DEFAULT_LABEL],
            command=lambda label: self._on_service_route_selected(
                LOCAL_PROXY_CUSTOM_ROUTE_ID,
                label,
            ),
            **combo_style(),
        )
        custom_route_combo.pack(side="left", fill="x", expand=True)
        custom_route_combo.set(SERVICE_ROUTE_DEFAULT_LABEL)
        self._service_route_combos[LOCAL_PROXY_CUSTOM_ROUTE_ID] = custom_route_combo

        self._custom_target_frame = ctk.CTkFrame(policy, fg_color="transparent")
        self._custom_target_frame.grid(row=6, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))

        self._routing_status_label = ctk.CTkLabel(
            policy,
            text=(
                "默认只代理 AI 相关域名；普通直连站点沿用系统 DNS。"
                "勾选内置站点或新增自定义目标后，会写入本机 mihomo 规则。"
            ),
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._routing_status_label.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        bind_wraplength(policy, self._routing_status_label, padding=20)

        node_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        node_frame.pack(fill="x", padx=14, pady=(0, 12))
        controls = ctk.CTkFrame(node_frame, fg_color="transparent")
        self._controls_grid = controls
        controls.pack(fill="x", padx=14, pady=14)
        controls.grid_columnconfigure(1, weight=1)
        controls.grid_columnconfigure(2, weight=1)

        self._subscription_heading = ctk.CTkLabel(
            controls,
            text="1 订阅来源",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        )
        self._subscription_heading.grid(row=0, column=0, columnspan=4, sticky="ew")
        self._subscription_profile_label_widget = ctk.CTkLabel(
            controls,
            text="订阅配置",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._subscription_profile_label_widget.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._subscription_profile_combo = ctk.CTkComboBox(
            controls,
            values=["新订阅"],
            command=self._on_subscription_profile_selected,
            **combo_style(),
        )
        self._subscription_profile_combo.grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        self._subscription_name_entry = ctk.CTkEntry(
            controls,
            placeholder_text="订阅名称，例如 香港家宽 / 备用机场",
            **input_style(),
        )
        self._subscription_name_entry.grid(row=1, column=2, sticky="ew", padx=(0, 8), pady=(8, 0))
        profile_actions = ctk.CTkFrame(controls, fg_color="transparent")
        self._profile_actions = profile_actions
        profile_actions.grid(row=1, column=3, sticky="e", pady=(8, 0))
        self._subscription_profile_save_button = ctk.CTkButton(
            profile_actions,
            text="保存",
            width=56,
            command=self._save_subscription_profile,
            **button_style("accent", compact=True),
        )
        self._subscription_profile_save_button.pack(side="left", padx=(0, 6))
        self._subscription_profile_delete_button = ctk.CTkButton(
            profile_actions,
            text="删除",
            width=56,
            command=self._delete_subscription_profile,
            **button_style("secondary", compact=True),
        )
        self._subscription_profile_delete_button.pack(side="left")
        self._subscription_link_label = ctk.CTkLabel(
            controls,
            text="订阅链接",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._subscription_link_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self._subscription_entry = ctk.CTkEntry(
            controls,
            placeholder_text="粘贴 Clash/mihomo 订阅链接；只保存在本机缓存",
            **input_style(),
        )
        self._subscription_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        sub_actions = ctk.CTkFrame(controls, fg_color="transparent")
        self._subscription_actions = sub_actions
        sub_actions.grid(row=2, column=3, sticky="e", pady=(8, 0))
        self._fetch_button = ctk.CTkButton(
            sub_actions,
            text="拉取订阅",
            width=86,
            command=self._fetch_subscription,
            **button_style("secondary", compact=True),
        )
        self._fetch_button.grid(row=0, column=0, sticky="ew")
        self._import_subscription_file_button = ctk.CTkButton(
            sub_actions,
            text="导入 YAML 节点",
            width=104,
            command=self._import_subscription_yaml,
            **button_style("secondary", compact=True),
        )
        self._import_subscription_file_button.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self._manual_hot_update_button = ctk.CTkButton(
            sub_actions,
            text="刷新并热更新",
            width=104,
            command=self._run_manual_hot_update,
            **button_style("accent", compact=True),
        )
        self._manual_hot_update_button.grid(row=0, column=2, sticky="ew", padx=(8, 0))
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
        self._auto_refresh_check.grid(row=0, column=3, sticky="w", padx=(8, 0))
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
        self._periodic_update_check.grid(row=1, column=0, sticky="w", pady=(6, 0))
        interval_group = ctk.CTkFrame(sub_actions, fg_color="transparent")
        interval_group.grid_columnconfigure(0, weight=1)
        self._periodic_update_entry = ctk.CTkEntry(
            interval_group,
            width=48,
            placeholder_text="60",
            **input_style(),
        )
        self._periodic_update_entry.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            interval_group,
            text="分钟",
            text_color=COLORS["muted"],
            font=font(12),
        ).grid(row=0, column=1, sticky="w", padx=(4, 0))
        interval_group.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))
        self._subscription_action_items = [
            self._fetch_button,
            self._import_subscription_file_button,
            self._manual_hot_update_button,
            self._auto_refresh_check,
            self._periodic_update_check,
            interval_group,
        ]
        self._cache_label = ctk.CTkLabel(
            controls,
            text="本机缓存: 未加载",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._cache_label.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        bind_wraplength(controls, self._cache_label, padding=20)

        self._node_selection_heading = ctk.CTkLabel(
            controls,
            text="2 节点选择",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        )
        self._node_selection_heading.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        self._subscription_nodes_label = ctk.CTkLabel(
            controls,
            text="订阅节点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._subscription_nodes_label.grid(row=5, column=0, sticky="w", pady=(8, 0))
        self._subscription_picker_host = ctk.CTkFrame(
            controls,
            height=360,
            fg_color=COLORS["field_bg"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        self._subscription_picker_host.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        self._subscription_picker_host.grid_propagate(False)
        ctk.CTkLabel(
            self._subscription_picker_host,
            text="节点选择器正在准备...",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(expand=True)
        node_actions = ctk.CTkFrame(controls, fg_color="transparent")
        self._node_actions = node_actions
        node_actions.grid(row=5, column=3, sticky="e", pady=(8, 0))
        ctk.CTkLabel(
            node_actions,
            text="检测",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(0, 4))
        self._latency_button = ctk.CTkButton(
            node_actions,
            text="测速勾选/筛选",
            width=118,
            command=self._measure_subscription_latencies,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._latency_button.pack(anchor="e", pady=(0, 6))
        self._quality_button = ctk.CTkButton(
            node_actions,
            text="检测勾选/筛选",
            width=118,
            command=self._measure_subscription_qualities,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._quality_button.pack(anchor="e", pady=(0, 6))
        self._quality_cancel_button = ctk.CTkButton(
            node_actions,
            text="取消检测",
            width=118,
            command=self._cancel_subscription_quality,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._quality_cancel_button.pack(anchor="e", pady=(0, 6))
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
            width=118,
            command=self._use_selected_subscription_node,
            state="disabled",
            **button_style("accent", compact=True),
        )
        self._use_node_button.pack(anchor="e")
        self._hot_update_node_button = ctk.CTkButton(
            node_actions,
            text="热更新当前",
            width=118,
            command=self._hot_update_selected_subscription_node,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._hot_update_node_button.pack(anchor="e", pady=(6, 0))
        ctk.CTkLabel(
            node_actions,
            text="设置",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(8, 4))
        self._quality_settings_button = ctk.CTkButton(
            node_actions,
            text="质量源/Key",
            width=118,
            command=self._open_proxy_quality_dialog,
            **button_style("primary", compact=True),
        )
        self._quality_settings_button.pack(anchor="e")
        self._subscription_action_hint_label = ctk.CTkLabel(
            node_actions,
            text=(
                "检测范围: -\n"
                "勾选优先；否则测当前筛选（无筛选即全部）\n"
                "自动选优不选香港；香港仍可手动使用\n"
                "质量源: -"
            ),
            text_color=COLORS["muted_soft"],
            font=font(11),
            width=126,
            anchor="e",
            justify="right",
            wraplength=126,
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
        self._selected_label.grid(row=6, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        bind_wraplength(controls, self._selected_label, padding=20)

        self._proxy_start_heading = ctk.CTkLabel(
            controls,
            text="3 启动本机代理",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        )
        self._proxy_start_heading.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        self._pending_node_label = ctk.CTkLabel(
            controls,
            text="待启动节点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._pending_node_label.grid(row=8, column=0, sticky="nw", pady=(8, 0))
        self._node_text_host = ctk.CTkFrame(
            controls,
            height=96,
            fg_color=COLORS["field_bg"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._node_text_host.grid(row=8, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        self._node_text_host.grid_propagate(False)
        ctk.CTkLabel(
            self._node_text_host,
            text="节点输入框正在准备...",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(expand=True)

        actions = ctk.CTkFrame(controls, fg_color="transparent")
        self._proxy_actions = actions
        actions.grid(row=8, column=3, sticky="ne", pady=(8, 0))
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
        self._cleanup_proxy_button = ctk.CTkButton(
            actions,
            text="清理脏代理",
            width=104,
            command=self._inspect_and_cleanup_stale_proxy,
            **button_style("warning", compact=True),
        )
        self._cleanup_proxy_button.pack(anchor="e", pady=(0, 6))
        self._core_update_button = ctk.CTkButton(
            actions,
            text="更新内核",
            width=104,
            command=self._update_local_proxy_core,
            **button_style("secondary", compact=True),
        )
        self._core_update_button.pack(anchor="e", pady=(0, 6))
        self._test_button = ctk.CTkButton(
            actions,
            text="测试连通",
            width=104,
            command=self._probe_local_proxy,
            **button_style("secondary", compact=True),
        )
        self._test_button.pack(anchor="e", pady=(0, 6))
        self._wsl_test_button = ctk.CTkButton(
            actions,
            text="测试/修复 WSL",
            width=104,
            command=self._test_and_repair_wsl_proxy,
            **button_style("secondary", compact=True),
        )
        self._wsl_test_button.pack(anchor="e", pady=(0, 6))
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
            text=(
                "默认从 17897 端口启动，端口占用时会自动顺延。开启 WSL 共享后只接入默认发行版；"
                "停止时同时恢复 Windows 设置并清理本工具写入的 WSL 环境入口。"
            ),
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._status_label.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        bind_wraplength(controls, self._status_label, padding=20)
        self.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self._schedule_responsive_layout(delay_ms=0)
        self._subscription_picker_after_id = self.after(1800, self._build_subscription_picker)

    def _logical_layout_width(self) -> int:
        width = self.winfo_width()
        try:
            scaling = float(self._get_widget_scaling())
        except (AttributeError, TypeError, ValueError):
            scaling = 1.0
        return max(1, round(width / scaling)) if scaling > 0 else max(1, width)

    def _schedule_responsive_layout(self, _event=None, delay_ms: int = 20) -> None:
        if self._responsive_after_id is not None:
            return

        def apply_layout():
            self._responsive_after_id = None
            try:
                if self.winfo_exists():
                    self._apply_responsive_layout()
            except Exception:
                pass

        try:
            self._responsive_after_id = self.after_idle(apply_layout) if delay_ms <= 0 else self.after(delay_ms, apply_layout)
        except Exception:
            self._responsive_after_id = None

    @staticmethod
    def _reset_grid_columns(frame, count: int) -> None:
        for column in range(count):
            frame.grid_columnconfigure(column, weight=0, minsize=0, uniform="")

    def _apply_responsive_layout(self) -> None:
        state = _local_proxy_tab_layout(self._logical_layout_width())
        if state == self._responsive_state:
            return
        self._responsive_state = state
        stacked, startup_columns, builtin_columns, subscription_action_columns, custom_stacked = state

        policy = self._policy_grid
        self._reset_grid_columns(policy, 4)
        if stacked:
            policy.grid_columnconfigure(0, weight=1)
        else:
            policy.grid_columnconfigure(1, weight=1)
            policy.grid_columnconfigure(2, weight=1)

        startup_box = self._startup_box
        self._reset_grid_columns(startup_box, 4)
        for column in range(startup_columns):
            startup_box.grid_columnconfigure(column, weight=1, uniform="proxy-startup")
        for index, widget in enumerate(self._startup_items):
            widget.grid(
                row=index // startup_columns,
                column=index % startup_columns,
                sticky="ew" if stacked else ("e" if index == len(self._startup_items) - 1 else "w"),
                padx=(0, 8) if index % startup_columns < startup_columns - 1 else 0,
                pady=(0, 6) if index < len(self._startup_items) - startup_columns else 0,
            )

        ai_route_box = self._ai_route_box
        self._reset_grid_columns(ai_route_box, 4)
        ai_route_columns = min(3, builtin_columns)
        for column in range(ai_route_columns):
            ai_route_box.grid_columnconfigure(column, weight=1, uniform="proxy-ai-routes")
        for index, widget in enumerate(self._ai_route_tiles):
            widget.grid(
                row=index // ai_route_columns,
                column=index % ai_route_columns,
                sticky="ew",
                padx=(0, 10) if index % ai_route_columns < ai_route_columns - 1 else 0,
                pady=(0, 6) if index < len(self._ai_route_tiles) - ai_route_columns else 0,
            )
        self._service_route_notice.grid(
            row=(len(self._ai_route_tiles) + ai_route_columns - 1) // ai_route_columns,
            column=0,
            columnspan=ai_route_columns,
            sticky="ew",
            pady=(4, 0),
        )

        builtin_box = self._builtin_box
        self._reset_grid_columns(builtin_box, 4)
        for column in range(builtin_columns):
            builtin_box.grid_columnconfigure(column, weight=1, uniform="proxy-builtins")
        for index, widget in enumerate(self._builtin_site_tiles):
            widget.grid(
                row=index // builtin_columns,
                column=index % builtin_columns,
                sticky="w",
                padx=(0, 14),
                pady=(0, 8),
            )

        custom_box = self._custom_box
        self._reset_grid_columns(custom_box, 2)
        custom_box.grid_columnconfigure(0, weight=1)
        self._custom_target_entry.grid(
            row=0,
            column=0,
            columnspan=2 if custom_stacked else 1,
            sticky="ew",
            padx=0 if custom_stacked else (0, 8),
        )
        self._custom_add_button.grid(
            row=1 if custom_stacked else 0,
            column=0 if custom_stacked else 1,
            columnspan=2 if custom_stacked else 1,
            sticky="ew" if custom_stacked else "e",
            pady=(6, 0) if custom_stacked else 0,
        )
        self._custom_route_row.grid(
            row=2 if custom_stacked else 1,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(6, 0),
        )

        if stacked:
            self._service_routes_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(12, 0))
            ai_route_box.grid(row=4, column=0, columnspan=4, sticky="ew", padx=0, pady=(8, 0))
            self._builtin_sites_label.grid(row=5, column=0, columnspan=4, sticky="w", pady=(12, 0))
            builtin_box.grid(row=6, column=0, columnspan=4, sticky="ew", padx=0, pady=(8, 0))
            self._custom_target_label.grid(row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))
            custom_box.grid(row=8, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            self._custom_target_frame.grid(row=9, column=0, columnspan=4, sticky="ew", padx=0, pady=(8, 0))
            self._routing_status_label.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        else:
            self._service_routes_label.grid(row=3, column=0, columnspan=1, sticky="nw", pady=(12, 0))
            ai_route_box.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
            self._builtin_sites_label.grid(row=4, column=0, columnspan=1, sticky="nw", pady=(12, 0))
            builtin_box.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
            self._custom_target_label.grid(row=5, column=0, columnspan=1, sticky="w", pady=(6, 0))
            custom_box.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
            self._custom_target_frame.grid(row=6, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(8, 0))
            self._routing_status_label.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))

        controls = self._controls_grid
        self._reset_grid_columns(controls, 4)
        if stacked:
            controls.grid_columnconfigure(0, weight=1)
        else:
            controls.grid_columnconfigure(1, weight=1)
            controls.grid_columnconfigure(2, weight=1)

        subscription_actions = self._subscription_actions
        self._reset_grid_columns(subscription_actions, 4)
        for column in range(subscription_action_columns):
            subscription_actions.grid_columnconfigure(column, weight=1, uniform="proxy-sub-actions")
        for index, widget in enumerate(self._subscription_action_items):
            widget.grid(
                row=index // subscription_action_columns,
                column=index % subscription_action_columns,
                sticky="ew",
                padx=(0 if index % subscription_action_columns == 0 else 8, 0),
                pady=(0 if index < subscription_action_columns else 6, 0),
            )

        if stacked:
            self._subscription_profile_label_widget.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
            self._subscription_profile_combo.grid(row=2, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            self._subscription_name_entry.grid(row=3, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            self._profile_actions.grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))
            self._subscription_link_label.grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
            self._subscription_entry.grid(row=6, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            subscription_actions.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(6, 0))
            self._cache_label.grid(row=8, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            self._node_selection_heading.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(14, 0))
            self._subscription_nodes_label.grid(row=10, column=0, columnspan=4, sticky="w", pady=(8, 0))
            self._subscription_picker_host.grid(row=11, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            self._node_actions.grid(row=12, column=0, columnspan=4, sticky="w", pady=(8, 0))
            self._selected_label.grid(row=13, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            self._proxy_start_heading.grid(row=14, column=0, columnspan=4, sticky="ew", pady=(14, 0))
            self._pending_node_label.grid(row=15, column=0, columnspan=4, sticky="nw", pady=(8, 0))
            self._node_text_host.grid(row=16, column=0, columnspan=4, sticky="ew", padx=0, pady=(6, 0))
            self._proxy_actions.grid(row=17, column=0, columnspan=4, sticky="w", pady=(8, 0))
            self._status_label.grid(row=18, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        else:
            self._subscription_profile_label_widget.grid(row=1, column=0, columnspan=1, sticky="w", pady=(8, 0))
            self._subscription_profile_combo.grid(row=1, column=1, columnspan=1, sticky="ew", padx=(8, 8), pady=(8, 0))
            self._subscription_name_entry.grid(row=1, column=2, columnspan=1, sticky="ew", padx=(0, 8), pady=(8, 0))
            self._profile_actions.grid(row=1, column=3, columnspan=1, sticky="e", pady=(8, 0))
            self._subscription_link_label.grid(row=2, column=0, columnspan=1, sticky="w", pady=(8, 0))
            self._subscription_entry.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
            subscription_actions.grid(row=2, column=3, columnspan=1, sticky="e", pady=(8, 0))
            self._cache_label.grid(row=3, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
            self._node_selection_heading.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(14, 0))
            self._subscription_nodes_label.grid(row=5, column=0, columnspan=1, sticky="w", pady=(8, 0))
            self._subscription_picker_host.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
            self._node_actions.grid(row=5, column=3, columnspan=1, sticky="e", pady=(8, 0))
            self._selected_label.grid(row=6, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
            self._proxy_start_heading.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(14, 0))
            self._pending_node_label.grid(row=8, column=0, columnspan=1, sticky="nw", pady=(8, 0))
            self._node_text_host.grid(row=8, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
            self._proxy_actions.grid(row=8, column=3, columnspan=1, sticky="ne", pady=(8, 0))
            self._status_label.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(10, 0))

    def _build_subscription_picker(self):
        self._subscription_picker_after_id = None
        if not is_active_tab(self):
            self._deferred_subscription_picker_pending = True
            return
        if recent_user_scroll(self, idle_ms=self.SCROLL_IDLE_BUILD_MS):
            self._subscription_picker_after_id = self.after(self.SCROLL_RETRY_BUILD_MS, self._build_subscription_picker)
            return
        self._deferred_subscription_picker_pending = False
        if self._subscription_picker or not self._subscription_picker_host:
            return
        try:
            for child in self._subscription_picker_host.winfo_children():
                child.destroy()
        except Exception:
            pass
        self._subscription_picker = ProxyNodePicker(
            self._subscription_picker_host,
            on_select=lambda _item: self._use_selected_subscription_node(show_message=False),
            on_scope_change=self._refresh_subscription_action_hint,
            on_group_quality=self._measure_subscription_qualities,
        )
        self._subscription_picker.pack(fill="x")
        self._subscription_picker.set_enabled(False)
        if self._subscription_nodes:
            self._set_subscription_nodes(self._subscription_nodes, preserve_key=self._selected_subscription_node_key())
        if not self._node_text and not self._node_text_after_id:
            self._node_text_after_id = self.after(360, self._build_node_text)

    def _build_node_text(self):
        self._node_text_after_id = None
        if not is_active_tab(self):
            self._deferred_node_text_pending = True
            return
        if recent_user_scroll(self, idle_ms=self.SCROLL_IDLE_BUILD_MS):
            self._node_text_after_id = self.after(self.SCROLL_RETRY_BUILD_MS, self._build_node_text)
            return
        self._deferred_node_text_pending = False
        if self._node_text or not self._node_text_host:
            return
        try:
            for child in self._node_text_host.winfo_children():
                child.destroy()
        except Exception:
            pass
        self._node_text = ctk.CTkTextbox(
            self._node_text_host,
            height=96,
            **textbox_style(monospace=True),
        )
        self._node_text.pack(fill="both", expand=True)
        if not self._initial_refresh_after_id and not self._saved_subscription_loaded:
            self._initial_refresh_after_id = self.after(420, self.refresh)

    def destroy(self):
        self._destroyed = True
        if self._quality_cancel_event is not None:
            self._quality_cancel_event.set()
        if self._responsive_after_id is not None:
            try:
                self.after_cancel(self._responsive_after_id)
            except Exception:
                pass
            self._responsive_after_id = None
        self._cancel_deferred_widget_builds()
        self._cancel_initial_refresh()
        self._cancel_saved_subscription_refresh()
        self._cancel_startup_refresh()
        self._cancel_periodic_update()
        super().destroy()

    def _resolve_ui_dispatch(self):
        try:
            dispatch = getattr(self.winfo_toplevel(), "_run_on_ui_thread", None)
        except Exception:
            return None
        return dispatch if callable(dispatch) else None

    def _run_on_ui_thread(self, callback):
        if getattr(self, "_destroyed", False):
            return
        dispatch = getattr(self, "_ui_dispatch", None)
        if callable(dispatch):
            dispatch(callback)
            return
        try:
            self.after(0, callback)
        except Exception:
            pass

    def _cancel_deferred_widget_builds(self):
        for attr in ("_subscription_picker_after_id", "_node_text_after_id"):
            after_id = getattr(self, attr, None)
            if not after_id:
                continue
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
            setattr(self, attr, None)

    def _cancel_initial_refresh(self):
        if not self._initial_refresh_after_id:
            return
        try:
            self.after_cancel(self._initial_refresh_after_id)
        except Exception:
            pass
        self._initial_refresh_after_id = None

    def _cancel_saved_subscription_refresh(self):
        if not self._saved_subscription_after_id:
            return
        try:
            self.after_cancel(self._saved_subscription_after_id)
        except Exception:
            pass
        self._saved_subscription_after_id = None

    def _suspend_background_work(self):
        if self._subscription_picker_after_id:
            self._deferred_subscription_picker_pending = True
        if self._node_text_after_id:
            self._deferred_node_text_pending = True
        if self._initial_refresh_after_id:
            self._deferred_initial_refresh_pending = True
        if self._saved_subscription_after_id:
            self._deferred_saved_subscription_pending = True
        self._cancel_deferred_widget_builds()
        self._cancel_initial_refresh()
        self._cancel_saved_subscription_refresh()

    def _iter_background_work_targets(self):
        yield self
        if self._subscription_picker:
            yield self._subscription_picker

    def _resume_background_work(self):
        if not is_active_tab(self):
            return
        if self._deferred_subscription_picker_pending and not self._subscription_picker:
            self._deferred_subscription_picker_pending = False
            self._schedule_after_once("_subscription_picker_after_id", self.SCROLL_RETRY_BUILD_MS, self._build_subscription_picker)
        if self._deferred_node_text_pending and not self._node_text:
            self._deferred_node_text_pending = False
            self._schedule_after_once("_node_text_after_id", self.SCROLL_RETRY_BUILD_MS, self._build_node_text)
        if self._deferred_initial_refresh_pending:
            self._deferred_initial_refresh_pending = False
            self._deferred_saved_subscription_pending = False
            self._schedule_after_once("_initial_refresh_after_id", self.SCROLL_RETRY_BUILD_MS, self.refresh)
        elif self._deferred_saved_subscription_pending:
            self._deferred_saved_subscription_pending = False
            self._schedule_after_once("_saved_subscription_after_id", self.SCROLL_RETRY_BUILD_MS, self._load_saved_subscription_ui)
        self._sync_shared_subscription_profile()

    def _current_subscription_profile_id(self) -> str:
        combo = getattr(self, "_subscription_profile_combo", None)
        if not combo:
            return ""
        try:
            label = combo.get()
        except Exception:
            return ""
        options = getattr(self, "_subscription_profile_options", {}) or {}
        return str(options.get(label) or "")

    def _sync_shared_subscription_profile(self) -> bool:
        """Reload the profile another proxy tab activated without fetching it."""

        if (
            getattr(self, "_busy", False)
            or getattr(self, "_periodic_update_running", False)
            or not getattr(self, "_saved_subscription_loaded", False)
        ):
            return False
        try:
            state = remote_proxy.load_proxy_subscription_state()
        except Exception:
            return False
        active_id = str(state.get("active_profile_id") or "")
        if active_id == self._current_subscription_profile_id():
            return False

        self._refresh_subscription_profile_options(state)
        self._apply_subscription_profile_inputs(state)
        self._latency_results = {}
        self._quality_results = {}
        self._prefer_quality_sort = False
        self._set_subscription_nodes(())
        self._saved_subscription_load_generation += 1
        generation = self._saved_subscription_load_generation
        if str(state.get("saved_path") or "").strip():
            self._load_subscription_cache_for_state(state, generation)
        else:
            self._set_cache_status("本机缓存: 当前分组尚无缓存", "warning")
            self._set_status("已同步其他代理页选择的订阅分组；当前分组尚无本机缓存。", "warning")
        return True

    def _schedule_after_once(self, attr: str, delay_ms: int, callback):
        if getattr(self, attr, None):
            return
        try:
            setattr(self, attr, self.after(max(1, int(delay_ms)), callback))
        except Exception:
            setattr(self, attr, None)
            callback()

    def refresh(self):
        self._initial_refresh_after_id = None
        if not is_active_tab(self):
            self._deferred_initial_refresh_pending = True
            return
        if recent_user_scroll(self, idle_ms=self.SCROLL_IDLE_BUILD_MS):
            self._schedule_after_once("_initial_refresh_after_id", self.SCROLL_RETRY_BUILD_MS, self.refresh)
            return
        self._sync_shared_subscription_profile()
        self._load_proxy_preferences_ui()
        self._cancel_saved_subscription_refresh()
        self._saved_subscription_after_id = self.after(220, self._load_saved_subscription_ui)

    def _set_status(self, message: str, severity: str = "info"):
        if not self._status_label:
            return
        color = {
            "busy": COLORS["accent"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._status_label.configure(text=safe_feedback_text(message), text_color=color)

    def _set_cache_status(self, message: str, severity: str = "info"):
        if not self._cache_label:
            return
        color = {
            "busy": COLORS["accent"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._cache_label.configure(text=safe_feedback_text(message), text_color=color)

    def _set_selected_summary(self, message: str, severity: str = "info"):
        if not self._selected_label:
            return
        color = {
            "busy": COLORS["accent"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._selected_label.configure(text=safe_feedback_text(message), text_color=color)

    def _set_routing_status(self, message: str, severity: str = "info"):
        if not self._routing_status_label:
            return
        color = {
            "busy": COLORS["accent"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._routing_status_label.configure(text=safe_feedback_text(message), text_color=color)

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self._fetch_button,
            getattr(self, "_import_subscription_file_button", None),
            getattr(self, "_manual_hot_update_button", None),
            self._latency_button,
            self._quality_button,
            self._use_node_button,
            self._hot_update_node_button,
            self._quality_settings_button,
            self._ping0_button,
            self._load_file_button,
            self._start_button,
            self._inspect_button,
            getattr(self, "_cleanup_proxy_button", None),
            getattr(self, "_core_update_button", None),
            self._test_button,
            getattr(self, "_wsl_test_button", None),
            self._stop_button,
            self._apply_routing_button,
            getattr(self, "_strict_privacy_check", None),
            getattr(self, "_wsl_share_check", None),
            getattr(self, "_wsl_optimize_button", None),
            self._subscription_profile_save_button,
            self._subscription_profile_delete_button,
        ):
            if not button:
                continue
            try:
                if button in (
                    self._use_node_button,
                    self._hot_update_node_button,
                    self._latency_button,
                    self._quality_button,
                    self._ping0_button,
                ) and not self._subscription_options:
                    button.configure(state="disabled")
                else:
                    button.configure(state=state)
            except Exception:
                pass
        if self._quality_cancel_button:
            try:
                can_cancel = bool(busy and self._quality_cancel_event is not None and not self._quality_cancel_event.is_set())
                self._quality_cancel_button.configure(state="normal" if can_cancel else "disabled")
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
        for widget in (
            self._subscription_profile_combo,
            self._subscription_name_entry,
            self._subscription_entry,
        ):
            if not widget:
                continue
            try:
                widget.configure(state=state)
            except Exception:
                pass
        for combo in getattr(self, "_service_route_combos", {}).values():
            try:
                combo.configure(state=state)
            except Exception:
                pass

    def _load_proxy_preferences_ui(self):
        self._preferences_load_generation += 1
        generation = self._preferences_load_generation
        self._set_routing_status("正在后台加载 Win11 代理偏好...")

        def run():
            try:
                payload = {
                    "ok": True,
                    "preferences": local_proxy.load_local_proxy_preferences(),
                    "error": "",
                }
            except Exception as e:
                payload = {"ok": False, "preferences": {}, "error": str(e)}

            def finish():
                if not self.winfo_exists() or generation != self._preferences_load_generation:
                    return
                if not payload["ok"]:
                    self._set_routing_status(f"加载 Win11 代理偏好失败: {payload['error']}", "error")
                    return
                self._apply_proxy_preferences_ui(payload["preferences"])

            self._run_on_ui_thread(finish)

        threading.Thread(target=run, name="local-proxy-preferences-load", daemon=True).start()

    def _apply_proxy_preferences_ui(self, preferences: dict):
        self._start_on_login_var.set(bool(preferences.get("start_on_login")))
        self._keep_running_on_exit_var.set(bool(preferences.get("keep_running_on_exit", True)))
        self._wsl_share_var.set(bool(preferences.get("share_to_wsl")))
        self._proxy_non_cn_var.set(bool(preferences.get("proxy_non_cn")))
        self._strict_privacy_var.set(bool(preferences.get("strict_privacy")))
        builtin_sites = preferences.get("builtin_sites") if isinstance(preferences.get("builtin_sites"), dict) else {}
        for site_id, var in self._builtin_site_vars.items():
            var.set(bool(builtin_sites.get(site_id)))
        self._service_route_bindings = dict(
            preferences.get("service_profile_bindings")
            if isinstance(preferences.get("service_profile_bindings"), dict)
            else {}
        )
        self._refresh_service_route_profile_options()
        self._render_custom_targets(preferences.get("custom_targets") or [])
        enabled_sites = sum(1 for enabled in builtin_sites.values() if enabled)
        enabled_custom = sum(1 for item in preferences.get("custom_targets") or [] if item.get("enabled", True))
        if preferences.get("strict_privacy"):
            mode = "严格隐私偏好已开启（运行态以“检查本机代理”的实际配置校验为准）"
            boundary = "应用成功后请重启 Codex/Claude Code/VS Code 和新终端"
        else:
            mode = "大陆境外 IP 走代理" if preferences.get("proxy_non_cn") else "仅规则命中的站点走代理"
            boundary = (
                "普通 DIRECT 站点沿用系统 DNS；应用层代理不保证阻止 WebRTC/UDP "
                "或忽略代理的程序绕过"
            )
        wsl_mode = "WSL 共享已开启" if preferences.get("share_to_wsl") else "WSL 共享未开启"
        binding_count = len(self._service_route_bindings)
        self._set_routing_status(
            f"当前规则: {mode}；{wsl_mode}；内置站点 {enabled_sites} 个，"
            f"自定义目标 {enabled_custom} 个；服务独立订阅绑定 {binding_count} 项。{boundary}。"
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

    def _wsl_share_selected(self) -> bool:
        value = getattr(self, "_wsl_share_var", None)
        try:
            return bool(value.get()) if value is not None else False
        except Exception:
            return False

    def _on_wsl_share_toggle(self):
        enabled = self._wsl_share_selected()
        # Do not visually commit a cross-environment change until confirmed.
        self._wsl_share_var.set(not enabled)
        if enabled:
            title = "开启 WSL 代理共享"
            action = "开启"
            message = (
                "开启后，启动 Win11 本机代理时会同时配置默认 WSL 发行版，让其中新启动的 "
                "Codex、Claude Code、普通终端和 VS Code Remote WSL 使用同一代理。\n\n"
                "WSL1/镜像网络继续只使用 127.0.0.1；传统 NAT/VirtioProxy 只对白名单中的 "
                "WSL 虚拟子网开放端口，必要时创建限定程序、端口和子网的防火墙规则。"
                "不会写入任何 API Key，也不会修改 Codex/Claude 登录文件。\n\n"
                "若当前是 NAT 且没有管理员权限，可先点“优化 WSL 网络”切换为微软推荐的镜像模式。"
            )
        else:
            title = "关闭 WSL 代理共享"
            action = "关闭"
            message = (
                "关闭后会把运行配置恢复为仅监听 Windows 回环地址，并删除本工具在默认 WSL "
                "发行版写入的 shell/VS Code Remote 环境入口和受管防火墙规则。\n\n"
                "已打开的 WSL 终端仍保留旧进程环境，关闭并重新打开后才会完全生效。"
            )

        def commit():
            self._wsl_share_var.set(enabled)

            def restore_visible_preference(_error=None):
                self._wsl_share_var.set(not enabled)
                self._load_proxy_preferences_ui()

            self._run_local_task(
                f"正在{action} WSL 代理共享并核对安全监听边界...",
                lambda: local_proxy.set_local_proxy_wsl_sharing_and_apply(enabled),
                f"{action} WSL 代理共享",
                on_success=lambda _result: self._load_proxy_preferences_ui(),
                on_error=restore_visible_preference,
                severity_from_result=lambda value: "warning"
                if any(marker in value for marker in ("未运行", "未安装", "不可达", "失败"))
                else "success",
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title=title,
            message=message,
            on_confirm=commit,
        )

    def _optimize_wsl_network(self):
        def optimize():
            message = local_proxy.optimize_wsl_networking()
            local_proxy.set_local_proxy_wsl_sharing(True)
            repair = local_proxy.repair_wsl_proxy_integration(full_probe=True)
            return f"{message}；已开启 WSL 代理共享偏好；{repair}"

        ConfirmDialog(
            self.winfo_toplevel(),
            title="优化 WSL 网络",
            message=(
                "将安全合并当前 Windows 用户的 .wslconfig：启用 networkingMode=mirrored、"
                "dnsTunneling=true 和 autoProxy=true，并保留其他段落与设置。若原文件存在，"
                "会先生成不覆盖旧文件的 .wslconfig.api-switcher.bak 备份。\n\n"
                "为使网络模式生效，确认后会执行 wsl --shutdown；所有正在运行的 WSL 终端、"
                "任务和 VS Code Remote WSL 会被关闭，请先保存其中的工作。重新打开 WSL 后，"
                "Linux 可通过 127.0.0.1 安全访问 Windows 代理，不需要向局域网开放端口。"
            ),
            on_confirm=lambda: self._run_local_task(
                "正在备份并优化 WSL 网络配置，然后安全关闭 WSL...",
                optimize,
                "优化 WSL 网络",
                on_success=lambda _result: self._load_proxy_preferences_ui(),
                severity_from_result=lambda value: "warning"
                if any(marker in value for marker in ("未能", "未运行", "不可达", "失败"))
                else "success",
                failure_hint="WSL 网络配置未完整应用；可根据提示手动检查 .wslconfig",
            ),
        )

    def _test_and_repair_wsl_proxy(self):
        self._run_local_task(
            "正在核对 WSL 网络模式、修复环境入口，并测试 Codex/Claude Code 无凭据路径...",
            lambda: local_proxy.repair_wsl_proxy_integration(full_probe=True),
            "测试/修复 WSL 代理",
            severity_from_result=lambda value: "success"
            if all(marker in value for marker in ("Codex 路径可达", "Claude Code 路径可达"))
            else "warning",
            failure_hint="未修改 Codex/Claude 登录状态；请按错误提示检查 WSL 网络或管理员权限",
        )

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

    def _strict_privacy_selected(self) -> bool:
        value = getattr(self, "_strict_privacy_var", None)
        try:
            return bool(value.get()) if value is not None else False
        except Exception:
            return False

    def _on_strict_privacy_toggle(self):
        enabled = self._strict_privacy_selected()
        # Keep the visible state unchanged until the confirmation commits it.
        self._strict_privacy_var.set(not enabled)

        if enabled:
            title = "开启严格隐私（应用层）"
            action = "开启"
            message = (
                "开启后，凡是已进入 mihomo 的公网流量都不会 DIRECT，订阅链接拉取也不会在代理失败后直连。\n\n"
                "这仍然不是 VPN/TUN，不能阻止忽略代理的程序、WebRTC/UDP 或系统 DNS/IPv6 绕过；"
                "也可能让部分需要直连的网站变慢或不可用。\n\n"
                "应用后请重启 Codex、Claude Code、VS Code 并打开新终端。"
            )
        else:
            title = "关闭严格隐私"
            action = "关闭"
            message = (
                "关闭后，只有命中代理规则的流量走节点，其他进入 mihomo 的流量可能 DIRECT；"
                "订阅链接在代理失败时也可恢复直连回退。\n\n"
                "应用后请重启 Codex、Claude Code、VS Code 并打开新终端。"
            )

        def commit():
            self._strict_privacy_var.set(enabled)

            def restore_visible_preference(_error=None):
                self._strict_privacy_var.set(not enabled)
                self._load_proxy_preferences_ui()

            self._run_local_task(
                f"正在{action}严格隐私并事务化更新运行配置...",
                lambda: local_proxy.set_local_proxy_strict_privacy_and_apply(enabled),
                f"{action}严格隐私",
                on_success=lambda _result: self._load_proxy_preferences_ui(),
                on_error=restore_visible_preference,
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title=title,
            message=message,
            on_confirm=commit,
        )

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

    def _set_entry_text(self, entry, value: str):
        if not entry:
            return
        entry.delete(0, "end")
        if value:
            entry.insert(0, value)

    def _service_route_label(self, service_id: str) -> str:
        key = str(service_id or "").strip()
        for item in (*LOCAL_PROXY_AI_SERVICES, *LOCAL_PROXY_BUILTIN_SITES):
            if str(item.get("id") or "") == key:
                return str(item.get("label") or key)
        if key == LOCAL_PROXY_CUSTOM_ROUTE_ID:
            return "自定义目标"
        return key or "服务"

    def _refresh_service_route_profile_options(self, profiles=None):
        if profiles is not None:
            self._subscription_profiles_snapshot = [
                dict(profile) for profile in profiles if isinstance(profile, dict)
            ]
        snapshots = list(getattr(self, "_subscription_profiles_snapshot", []) or [])
        values = [SERVICE_ROUTE_DEFAULT_LABEL]
        mapping = {SERVICE_ROUTE_DEFAULT_LABEL: ""}
        labels_by_id = {"": SERVICE_ROUTE_DEFAULT_LABEL}
        seen = {SERVICE_ROUTE_DEFAULT_LABEL}
        for index, profile in enumerate(snapshots, 1):
            profile_id = str(profile.get("id") or "").strip()
            if not profile_id:
                continue
            label = self._subscription_profile_label(profile)
            if not str(profile.get("saved_path") or "").strip():
                label = f"{label} · 未拉取"
            candidate = label
            suffix = 2
            while candidate in seen:
                candidate = f"{label} ({suffix})"
                suffix += 1
            label = candidate
            seen.add(label)
            values.append(label)
            mapping[label] = profile_id
            labels_by_id[profile_id] = label
        self._service_route_profile_options = mapping
        self._service_route_profile_labels = labels_by_id
        self._service_route_combo_loading = True
        try:
            bindings = dict(getattr(self, "_service_route_bindings", {}) or {})
            for service_id, combo in getattr(self, "_service_route_combos", {}).items():
                profile_id = str(bindings.get(service_id) or "").strip()
                selected_label = labels_by_id.get(
                    profile_id,
                    SERVICE_ROUTE_MISSING_LABEL if profile_id else SERVICE_ROUTE_DEFAULT_LABEL,
                )
                combo_values = list(values)
                if selected_label == SERVICE_ROUTE_MISSING_LABEL:
                    combo_values.append(SERVICE_ROUTE_MISSING_LABEL)
                try:
                    combo.configure(values=combo_values)
                    combo.set(selected_label)
                except Exception:
                    continue
        finally:
            self._service_route_combo_loading = False

    def _on_service_route_selected(self, service_id: str, label: str):
        if self._service_route_combo_loading:
            return
        service_key = str(service_id or "").strip()
        selected_label = str(label or "").strip()
        if self._busy:
            self._refresh_service_route_profile_options()
            show_toast(
                self.winfo_toplevel(),
                "当前代理操作正在运行，请稍后再修改服务线路",
                is_error=True,
            )
            return
        if selected_label not in self._service_route_profile_options:
            self._refresh_service_route_profile_options()
            message = "原订阅绑定已失效，请从现有订阅或“跟随当前节点”中重新选择"
            self._set_routing_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        profile_id = self._service_route_profile_options[selected_label]
        if str(self._service_route_bindings.get(service_key) or "") == profile_id:
            return
        service_label = self._service_route_label(service_key)

        self._run_local_task(
            f"正在保存并应用 {service_label} 的订阅线路...",
            lambda: local_proxy.set_local_proxy_service_profile_binding_and_apply(
                service_key,
                profile_id,
            ),
            "应用服务订阅分流",
            on_success=lambda _result: self._load_proxy_preferences_ui(),
            on_error=lambda _error=None: self._load_proxy_preferences_ui(),
            failure_hint="旧运行配置与原服务绑定已保留，不会切换账号登录状态",
        )

    def _subscription_profile_label(self, profile: dict) -> str:
        name = str(profile.get("name") or "未命名订阅").strip()
        url = str(profile.get("url") or "").strip()
        host = ""
        if url:
            try:
                from urllib import parse as urlparse

                host = urlparse.urlparse(url).netloc.split("@")[-1].split(":")[0]
            except Exception:
                host = ""
        if host and host.casefold() not in name.casefold():
            return f"{name} · {host}"
        return name

    def _refresh_subscription_profile_options(self, state: dict | None = None):
        profiles = remote_proxy.list_proxy_subscription_profiles()
        self._refresh_service_route_profile_options(profiles)
        active_id = str((state or remote_proxy.load_proxy_subscription_state()).get("active_profile_id") or "")
        values = []
        mapping = {}
        seen = set()
        active_label = "新订阅"
        for index, profile in enumerate(profiles, 1):
            label = self._subscription_profile_label(profile)
            if label in seen:
                label = f"{label} ({index})"
            seen.add(label)
            values.append(label)
            mapping[label] = str(profile.get("id") or "")
            if profile.get("id") == active_id:
                active_label = label
        self._subscription_profile_options = mapping
        if self._subscription_profile_combo:
            self._subscription_profile_loading = True
            try:
                self._subscription_profile_combo.configure(values=values or ["新订阅"])
                self._subscription_profile_combo.set(active_label if values else "新订阅")
            finally:
                self._subscription_profile_loading = False

    def _apply_subscription_profile_inputs(self, state: dict):
        profile = remote_proxy.active_proxy_subscription_profile()
        self._set_entry_text(self._subscription_name_entry, str(profile.get("name") or ""))
        self._set_entry_text(self._subscription_entry, str(state.get("url") or ""))

    def _begin_subscription_profile_mutation(
        self,
        action: str,
        *,
        show_message: bool = True,
    ) -> tuple[bool, bool]:
        """Reserve profile state unless this tab already owns the hot-update flight."""

        if bool(getattr(self, "_subscription_hot_update_lock_owned", False)):
            return True, False
        if remote_proxy.try_acquire_proxy_subscription_hot_update():
            return True, True
        message = f"另一个订阅热更新正在进行，请稍后再{action}订阅配置"
        self._set_status(message, "warning")
        if show_message:
            show_toast(self.winfo_toplevel(), message, is_error=True)
        return False, False

    def _save_subscription_profile(self, show_message: bool = True):
        if self._busy:
            if show_message:
                show_toast(self.winfo_toplevel(), "当前代理操作正在运行，请稍后再保存订阅配置", is_error=True)
            return None
        url = self._subscription_url_input()
        if not url:
            active = remote_proxy.active_proxy_subscription_profile()
            if active.get("source_path"):
                return self._rename_local_subscription_profile(active, show_message=show_message)
            message = "请先填写订阅链接，再保存订阅配置"
            self._set_status(message, "warning")
            if show_message:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            return None
        name = self._subscription_name_entry.get().strip() if self._subscription_name_entry else ""
        allowed, release_needed = self._begin_subscription_profile_mutation(
            "保存",
            show_message=show_message,
        )
        if not allowed:
            return None
        try:
            profile = remote_proxy.save_proxy_subscription_profile(name, url, activate=True)
            state = remote_proxy.load_proxy_subscription_state()
        except Exception as exc:
            message = f"订阅配置保存失败: {exc}"
            self._set_status(message, "error")
            if show_message:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            return None
        finally:
            if release_needed:
                remote_proxy.release_proxy_subscription_hot_update()
        self._refresh_subscription_profile_options(state)
        self._apply_subscription_profile_inputs(state)
        if show_message:
            message = f"已保存订阅配置: {profile.get('name') or '未命名订阅'}"
            self._set_status(message, "success")
            show_toast(self.winfo_toplevel(), message)
        return profile

    def _rename_local_subscription_profile(self, profile: dict, *, show_message: bool):
        profile_id = str(profile.get("id") or "")
        name = self._subscription_name_entry.get().strip() if self._subscription_name_entry else ""
        if not name:
            name = Path(str(profile.get("source_path") or "")).stem or "本地 Clash 配置"
        allowed, release_needed = self._begin_subscription_profile_mutation(
            "保存",
            show_message=show_message,
        )
        if not allowed:
            return None
        try:
            updated = remote_proxy.rename_proxy_subscription_profile(profile_id, name)
            state = remote_proxy.load_proxy_subscription_state()
        except Exception as exc:
            message = f"本地 Clash 配置保存失败: {exc}"
            self._set_status(message, "error")
            if show_message:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            return None
        finally:
            if release_needed:
                remote_proxy.release_proxy_subscription_hot_update()
        self._refresh_subscription_profile_options(state)
        self._apply_subscription_profile_inputs(state)
        if show_message:
            message = f"已保存本地 Clash 配置名称: {updated.get('name')}"
            self._set_status(message, "success")
            show_toast(self.winfo_toplevel(), message)
        return updated

    def _delete_subscription_profile(self):
        if self._busy:
            show_toast(self.winfo_toplevel(), "当前代理操作正在运行，请稍后再删除订阅配置", is_error=True)
            return
        if not self._subscription_profile_combo:
            return
        label = self._subscription_profile_combo.get()
        profile_id = self._subscription_profile_options.get(label)
        if not profile_id:
            message = "当前没有可删除的订阅配置"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        try:
            bound_service_ids = local_proxy.local_proxy_service_bindings_for_profile(
                profile_id
            )
        except Exception as exc:
            message = (
                "无法确认该订阅是否正被服务分流使用，已取消删除: "
                f"{exc}"
            )
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        bound_services = [
            self._service_route_label(service_id)
            for service_id in bound_service_ids
        ]
        if bound_services:
            message = (
                f"该订阅正被 {'、'.join(bound_services)} 使用；"
                "请先把对应线路改为“跟随当前节点”或其他订阅，再删除"
            )
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        def do_delete():
            allowed, release_needed = self._begin_subscription_profile_mutation("删除")
            if not allowed:
                return
            try:
                try:
                    latest_bound_ids = (
                        local_proxy.local_proxy_service_bindings_for_profile(
                            profile_id
                        )
                    )
                except Exception as exc:
                    message = (
                        "删除前无法重新确认该订阅的服务绑定状态，已取消删除: "
                        f"{exc}"
                    )
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return
                if latest_bound_ids:
                    latest_labels = "、".join(
                        self._service_route_label(service_id)
                        for service_id in latest_bound_ids
                    )
                    message = (
                        f"删除前检测到该订阅已被 {latest_labels} 使用；"
                        "已取消删除，请先调整服务线路"
                    )
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return
                remote_proxy.delete_proxy_subscription_profile(profile_id)
                state = remote_proxy.load_proxy_subscription_state()
            except Exception as exc:
                message = f"订阅配置删除失败: {exc}"
                self._set_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return
            finally:
                if release_needed:
                    remote_proxy.release_proxy_subscription_hot_update()
            self._refresh_subscription_profile_options(state)
            self._apply_subscription_profile_inputs(state)
            self._latency_results = {}
            self._quality_results = {}
            self._prefer_quality_sort = False
            self._set_subscription_nodes(())
            message = "已删除订阅配置"
            self._set_status(message, "success")
            show_toast(self.winfo_toplevel(), message)
            if state.get("saved_path"):
                self._saved_subscription_load_generation += 1
                self._load_subscription_cache_for_state(state, self._saved_subscription_load_generation)

        ConfirmDialog(
            self.winfo_toplevel(),
            title="删除订阅配置",
            message=f"将删除订阅配置“{label}”及其本地缓存引用。确定继续吗？",
            on_confirm=do_delete,
        )

    def _on_subscription_profile_selected(self, label: str):
        if self._subscription_profile_loading:
            return
        if self._busy:
            show_toast(self.winfo_toplevel(), "当前代理操作正在运行，请稍后再切换订阅配置", is_error=True)
            return
        profile_id = self._subscription_profile_options.get(str(label or ""))
        if not profile_id:
            return
        allowed, release_needed = self._begin_subscription_profile_mutation("切换")
        if not allowed:
            self._refresh_subscription_profile_options()
            return
        try:
            remote_proxy.set_active_proxy_subscription_profile(profile_id)
            state = remote_proxy.load_proxy_subscription_state()
        except Exception as exc:
            try:
                self._refresh_subscription_profile_options()
            except Exception:
                pass
            message = f"订阅配置切换失败: {exc}"
            self._set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        finally:
            if release_needed:
                remote_proxy.release_proxy_subscription_hot_update()
        self._apply_subscription_profile_inputs(state)
        self._latency_results = {}
        self._quality_results = {}
        self._prefer_quality_sort = False
        self._set_subscription_nodes(())
        self._saved_subscription_load_generation += 1
        self._load_subscription_cache_for_state(state, self._saved_subscription_load_generation)

    def _load_saved_subscription_ui(self):
        self._saved_subscription_after_id = None
        if not is_active_tab(self):
            self._deferred_saved_subscription_pending = True
            return
        if self._saved_subscription_loaded:
            return
        self._saved_subscription_loaded = True
        self._saved_subscription_load_generation += 1
        generation = self._saved_subscription_load_generation
        self._set_cache_status("本机缓存: 正在后台读取订阅状态...")

        def run():
            try:
                state = remote_proxy.load_proxy_subscription_state()
                payload = {
                    "ok": True,
                    "state": state,
                    "auto_refresh": remote_proxy.proxy_subscription_auto_refresh_enabled("local"),
                    "error": "",
                }
            except Exception as e:
                payload = {"ok": False, "state": {}, "auto_refresh": False, "error": str(e)}

            def finish():
                if not self.winfo_exists() or generation != self._saved_subscription_load_generation:
                    return
                if not payload["ok"]:
                    self._saved_subscription_loaded = False
                    self._set_cache_status("本机缓存: 订阅状态读取失败", "error")
                    self._set_status(f"读取 Win11 代理订阅状态失败: {payload['error']}", "error")
                    return
                state = payload["state"]
                self._refresh_subscription_profile_options(state)
                self._apply_subscription_profile_inputs(state)
                auto_refresh = bool(payload["auto_refresh"])
                periodic_update = bool(state.get("local_periodic_update_enabled"))
                interval_minutes = str(state.get("local_periodic_update_interval_minutes") or "60")
                self._auto_refresh_var.set(auto_refresh)
                self._periodic_update_var.set(periodic_update)

                if self._periodic_update_entry:
                    self._periodic_update_entry.delete(0, "end")
                    self._periodic_update_entry.insert(0, interval_minutes)

                if not str(state.get("saved_path") or "").strip():
                    if str(state.get("url") or "").strip() and auto_refresh:
                        self._schedule_startup_refresh()
                    self._schedule_periodic_update(initial=True)
                    return

                self._load_subscription_cache_for_state(state, generation, auto_refresh=auto_refresh, schedule_periodic=True)

            self._run_on_ui_thread(finish)

        threading.Thread(target=run, name="local-proxy-subscription-state-load", daemon=True).start()

    def _load_subscription_cache_for_state(
        self,
        state: dict,
        generation: int,
        *,
        auto_refresh: bool = False,
        schedule_periodic: bool = False,
    ):
        url = str(state.get("url") or "").strip()
        saved_path = str(state.get("saved_path") or "").strip()
        if not saved_path:
            return
        self._set_cache_status("本机缓存: 正在后台恢复订阅...", "info")
        self._set_status("正在后台恢复本机缓存订阅；页面可先操作。")

        def run():
            cached = remote_proxy.load_cached_proxy_subscription(state)
            payload = {
                "cached": cached,
                "latencies": remote_proxy.load_proxy_subscription_latencies(state) if cached and cached.nodes else {},
                "qualities": remote_proxy.load_proxy_subscription_qualities(state) if cached and cached.nodes else {},
            }

            def finish():
                if not self.winfo_exists() or generation != self._saved_subscription_load_generation:
                    return
                cached_result = payload["cached"]
                if cached_result and cached_result.nodes:
                    self._latency_results = payload["latencies"]
                    self._quality_results = payload["qualities"]
                    self._prefer_quality_sort = bool(self._quality_results)
                    selected_key = str(state.get("selected_node_key") or "")
                    self._set_subscription_nodes(cached_result.nodes, preserve_key=selected_key)
                    self._select_subscription_node_by_key(selected_key)
                    source_label = "本地 YAML" if state.get("source_path") and not url else "订阅"
                    updated_at = state.get("last_fetched_at") or "-"
                    self._set_cache_status(
                        f"本机缓存: {len(cached_result.nodes)} 个节点；{source_label}更新 {updated_at}",
                        "success",
                    )
                    self._set_status(
                        f"已加载{source_label}缓存: {len(cached_result.nodes)} 个节点；更新时间 {updated_at}"
                    )
                else:
                    self._set_cache_status("本机缓存: 未找到可用节点", "warning")
                    self._set_status("未找到可用本机缓存；可重新拉取订阅或导入本地 Clash YAML。", "warning")

                if url and auto_refresh:
                    self._schedule_startup_refresh()
                if schedule_periodic:
                    self._schedule_periodic_update(initial=True)

            self._run_on_ui_thread(finish)

        threading.Thread(target=run, name="local-proxy-cache-load", daemon=True).start()

    def _select_subscription_node_by_key(self, node_key: str) -> bool:
        if not node_key or not self._subscription_picker:
            return False
        return self._subscription_picker.select_by_key(node_key)

    def _selected_subscription_item(self):
        if not self._subscription_picker:
            return None
        try:
            return self._subscription_picker.selected_item()
        except Exception:
            return None

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
        self._startup_refresh_after_id = self.after(self.STARTUP_REFRESH_DELAY_MS, self._run_startup_refresh)

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

    def _run_manual_hot_update(self):
        self._start_subscription_hot_update(manual=True)

    def _run_periodic_update(self):
        self._periodic_update_after_id = None
        if not bool(self._periodic_update_var.get()):
            return
        self._start_subscription_hot_update(manual=False)

    def _start_subscription_hot_update(self, *, manual: bool):
        mode = "手动" if manual else "定时"
        if self._periodic_update_running or self._busy:
            if manual:
                message = "已有代理操作或热更新正在进行，请稍等"
                self._set_status(message, "warning")
                show_toast(self.winfo_toplevel(), message, is_error=True)
            self._schedule_periodic_update()
            return
        url = self._subscription_url_input()
        if not url:
            message = f"Win11 代理{mode}热更新跳过：尚未设置订阅链接。"
            self._set_status(message, "warning")
            if manual:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            self._schedule_periodic_update()
            return
        lock_owned = False
        busy_started = False
        release_guard = threading.Lock()
        lock_released = False

        def release_lock_once():
            nonlocal lock_released
            with release_guard:
                if lock_released:
                    return
                self._subscription_hot_update_lock_owned = False
                remote_proxy.release_proxy_subscription_hot_update()
                lock_released = True

        try:
            if not remote_proxy.try_acquire_proxy_subscription_hot_update():
                message = "另一个订阅热更新正在进行，请稍后再试"
                self._set_status(message, "warning")
                if manual:
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                self._schedule_periodic_update()
                return
            lock_owned = True
            self._subscription_hot_update_lock_owned = True

            # Saving may activate a different subscription profile. Keep that state
            # transition inside the same cross-tab reservation as fetch + apply.
            profile = self._save_subscription_profile(show_message=False)
            if not profile:
                self._schedule_periodic_update()
                return
            profile_id = str(profile.get("id") or "")
            if not profile_id:
                raise RuntimeError("订阅分组 ID 为空")

            self._cancel_periodic_update()
            self._saved_subscription_load_generation += 1
            generation = self._saved_subscription_load_generation
            self._periodic_update_running = True
            busy_started = True
            self._set_busy(True)
            self._set_cache_status(f"本机缓存: {mode}热更新中...")
            self._set_status(f"正在{mode}拉取订阅，并尝试无重启更新运行中的本机代理...")

            def run():
                payload = {
                    "fetch_ok": False,
                    "result": None,
                    "fetch_error": "",
                    "apply": "",
                    "apply_error": "",
                    "profile": dict(profile),
                    "profile_changed": False,
                    "bound_route": False,
                }
                try:
                    try:
                        allow_direct_fallback = (
                            local_proxy.local_proxy_subscription_direct_fallback_allowed()
                        )
                        result = remote_proxy.fetch_proxy_subscription(
                            url,
                            profile_id=profile_id,
                            activate=False,
                            allow_direct_fallback=allow_direct_fallback,
                            recovery_proxy_provider=(
                                local_proxy.local_proxy_subscription_recovery_session
                            ),
                        )
                    except Exception as exc:
                        payload["fetch_error"] = str(exc)
                    else:
                        payload["fetch_ok"] = True
                        payload["result"] = result
                        try:
                            state = remote_proxy.load_proxy_subscription_state()
                            profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
                            profile_snapshot = dict(profiles.get(profile_id) or profile)
                            payload["profile"] = profile_snapshot
                            payload["profile_changed"] = (
                                str(state.get("active_profile_id") or "") != profile_id
                            )
                            quality_results = dict(profile_snapshot.get("node_qualities") or {})
                            try:
                                bound_services = (
                                    local_proxy.local_proxy_service_bindings_for_profile(
                                        profile_id
                                    )
                                )
                                payload["bound_route"] = bool(bound_services)
                                if bound_services:
                                    payload["apply"] = (
                                        local_proxy.refresh_running_local_service_routes_from_subscription(
                                            result.nodes,
                                            profile_id=profile_id,
                                        )
                                    )
                                elif payload["profile_changed"]:
                                    payload["apply"] = (
                                        "订阅分组已切换，仅刷新原分组缓存，"
                                        "未更新运行中的本机主节点"
                                    )
                                else:
                                    payload["apply"] = local_proxy.refresh_running_local_ai_proxy_from_subscription(
                                        result.nodes,
                                        quality_results=quality_results,
                                        profile_id=profile_id,
                                    )
                            except Exception as exc:
                                payload["apply_error"] = str(exc)
                            try:
                                refreshed_state = remote_proxy.load_proxy_subscription_state()
                                refreshed_profiles = (
                                    refreshed_state.get("profiles")
                                    if isinstance(refreshed_state.get("profiles"), dict)
                                    else {}
                                )
                                payload["profile"] = dict(
                                    refreshed_profiles.get(profile_id) or profile_snapshot
                                )
                            except Exception:
                                pass
                        except Exception as exc:
                            payload["apply_error"] = f"无法确认当前订阅分组，已跳过运行态更新: {exc}"
                finally:
                    release_lock_once()

                def finish():
                    if not self.winfo_exists():
                        return
                    self._periodic_update_running = False
                    self._set_busy(False)
                    try:
                        if generation != self._saved_subscription_load_generation:
                            return
                        if not payload["fetch_ok"]:
                            message = f"Win11 代理{mode}热更新拉取失败: {payload['fetch_error']}"
                            self._set_cache_status(
                                f"本机缓存: {mode}热更新拉取失败，继续使用已有节点",
                                "warning",
                            )
                            self._set_status(message, "error" if manual else "warning")
                            if manual:
                                show_toast(self.winfo_toplevel(), message, is_error=True)
                            return

                        result = payload["result"]
                        proxy_warning = str(getattr(result, "proxy_warning", "") or "").strip()
                        profile_snapshot = payload.get("profile") or {}
                        if payload["profile_changed"]:
                            if payload["apply_error"]:
                                message = (
                                    f"Win11 代理{mode}订阅已刷新，但独立服务线路更新失败，"
                                    f"已保留原运行配置: {payload['apply_error']}"
                                )
                            else:
                                message = f"Win11 代理{mode}热更新完成；{payload['apply']}"
                            if proxy_warning:
                                message = f"{proxy_warning}；{message}"
                            self._set_status(message, "warning")
                            if manual:
                                show_toast(self.winfo_toplevel(), message, is_error=True)
                            return

                        current_state = remote_proxy.load_proxy_subscription_state()
                        if str(current_state.get("active_profile_id") or "") != profile_id:
                            if payload["bound_route"] and payload["apply_error"]:
                                message = (
                                    f"Win11 代理{mode}订阅已刷新，但独立服务线路更新失败，"
                                    f"已保留原运行配置: {payload['apply_error']}"
                                )
                            elif payload["bound_route"]:
                                message = (
                                    f"Win11 代理{mode}热更新已完成；{payload['apply']}；"
                                    "当前订阅页面已切换，界面保持新分组"
                                )
                            else:
                                message = (
                                    f"Win11 代理{mode}热更新已完成，但当前分组已切换；"
                                    "新节点保存在原分组，当前界面保持不变"
                                )
                            if proxy_warning:
                                message = f"{proxy_warning}；{message}"
                            self._set_status(message, "warning")
                            if manual:
                                show_toast(self.winfo_toplevel(), message, is_error=True)
                            return
                        current_profiles = (
                            current_state.get("profiles")
                            if isinstance(current_state.get("profiles"), dict)
                            else {}
                        )
                        profile_snapshot = dict(current_profiles.get(profile_id) or profile_snapshot)

                        self._latency_results = dict(profile_snapshot.get("node_latencies") or {})
                        self._quality_results = dict(profile_snapshot.get("node_qualities") or {})
                        self._prefer_quality_sort = bool(self._quality_results)
                        selected_key = str(
                            profile_snapshot.get("selected_node_key")
                            or self._selected_subscription_node_key()
                        )
                        self._set_subscription_nodes(result.nodes, preserve_key=selected_key)
                        self._set_cache_status(
                            f"本机缓存: {mode}热更新已保存 {len(result.nodes)} 个节点",
                            "success",
                        )
                        if payload["apply_error"]:
                            message = (
                                "Win11 代理订阅已刷新；运行中代理热更新失败，已保留原节点: "
                                f"{payload['apply_error']}"
                            )
                            severity = "warning"
                        else:
                            message = f"Win11 代理{mode}热更新完成；{payload['apply']}"
                            severity = self._periodic_update_message_severity(payload["apply"])
                        if proxy_warning:
                            message = f"{proxy_warning}；{message}"
                            severity = "warning"
                        self._set_status(message, severity)
                        if manual:
                            show_toast(
                                self.winfo_toplevel(),
                                message,
                                is_error=severity in {"warning", "error"},
                                severity=severity,
                            )
                    except Exception as exc:
                        message = f"订阅已刷新，但界面同步失败: {exc}"
                        self._set_status(message, "warning")
                        if manual:
                            show_toast(self.winfo_toplevel(), message, is_error=True)
                    finally:
                        self._schedule_periodic_update()

                self._run_on_ui_thread(finish)

            threading.Thread(
                target=run,
                name=f"local-proxy-{mode}-hot-update",
                daemon=True,
            ).start()
            lock_owned = False
        except Exception as exc:
            self._periodic_update_running = False
            if busy_started:
                self._set_busy(False)
            message = f"Win11 代理{mode}热更新启动失败: {exc}"
            self._set_cache_status(f"本机缓存: {mode}热更新未启动", "warning")
            self._set_status(message, "error" if manual else "warning")
            if manual:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            self._schedule_periodic_update()
        finally:
            if lock_owned:
                release_lock_once()

    def _periodic_update_message_severity(self, message: str) -> str:
        text = str(message or "")
        if any(
            marker in text
            for marker in ("失败", "未完全", "跳过", "不可用", "没有测到", "已保留", "不合格")
        ):
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
            options[remote_proxy.proxy_subscription_node_key(item)] = item
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
        if self._hot_update_node_button:
            self._hot_update_node_button.configure(state="normal" if options and not self._busy else "disabled")
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
        scope = self._subscription_batch_scope_label()
        settings = network_diagnostic_settings.load_settings()
        configured_services = settings.enabled_services()
        services = remote_proxy.proxy_quality_effective_services(settings, configured_services)
        skipped_services = [service for service in configured_services if service not in services]
        if services:
            source_text = (
                "可执行质量源: "
                + remote_proxy.quality_source_label_from_settings(settings, services)
            )
            if skipped_services:
                source_text += (
                    "\n缺 Key 已跳过: "
                    + remote_proxy.quality_source_label_from_settings(settings, skipped_services)
                )
        else:
            source_text = "可执行质量源: 无"
            if skipped_services:
                source_text += (
                    "\n已启用但缺 Key: "
                    + remote_proxy.quality_source_label_from_settings(settings, skipped_services)
                )
            else:
                source_text += "\n请先启用检测源"
        color = COLORS["warning"] if skipped_services or not services else COLORS["muted_soft"]
        self._subscription_action_hint_label.configure(
            text=(
                f"检测范围: {scope}\n"
                "勾选优先；否则测当前筛选（无筛选即全部）\n"
                "自动选优不选香港；香港仍可手动使用\n"
                f"{source_text}"
            ),
            text_color=color,
        )

    def _fetch_subscription(self, auto: bool = False, show_message: bool = True):
        if self._busy or self._periodic_update_running:
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
        try:
            previous_active_id = str(
                remote_proxy.load_proxy_subscription_state().get("active_profile_id") or ""
            )
        except Exception:
            previous_active_id = ""
        profile = self._save_subscription_profile(show_message=False)
        if not profile:
            return

        self._saved_subscription_load_generation += 1
        generation = self._saved_subscription_load_generation
        profile_id = str(profile.get("id") or "")
        self._set_busy(True)
        self._set_cache_status("本机缓存: 正在刷新订阅..." if auto else "本机缓存: 正在拉取订阅...")
        self._set_status("正在自动刷新订阅..." if auto else "正在拉取订阅并解析节点...")

        def run():
            try:
                allow_direct_fallback = (
                    local_proxy.local_proxy_subscription_direct_fallback_allowed()
                )
                result = remote_proxy.fetch_proxy_subscription(
                    url,
                    profile_id=profile_id,
                    activate=False,
                    allow_direct_fallback=allow_direct_fallback,
                    recovery_proxy_provider=(
                        local_proxy.local_proxy_subscription_recovery_session
                    ),
                )
                route_apply = ""
                route_apply_error = ""
                try:
                    if local_proxy.local_proxy_service_bindings_for_profile(profile_id):
                        route_apply = (
                            local_proxy.refresh_running_local_service_routes_from_subscription(
                                result.nodes,
                                profile_id=profile_id,
                            )
                        )
                except Exception as exc:
                    route_apply_error = str(exc)
                payload = {
                    "ok": True,
                    "result": result,
                    "error": None,
                    "cached": None,
                    "cached_state": {},
                    "latencies": {},
                    "qualities": {},
                    "route_apply": route_apply,
                    "route_apply_error": route_apply_error,
                }
            except Exception as e:
                cached = None
                cached_state = {}
                latencies = {}
                qualities = {}
                try:
                    failed_state = remote_proxy.load_proxy_subscription_state()
                    profiles = failed_state.get("profiles")
                    if isinstance(profiles, dict):
                        cached_state = dict(profiles.get(profile_id) or {})
                    if cached_state:
                        cached = remote_proxy.load_cached_proxy_subscription(cached_state)
                        if cached and cached.nodes:
                            latencies = remote_proxy.load_proxy_subscription_latencies(cached_state)
                            qualities = remote_proxy.load_proxy_subscription_qualities(cached_state)
                except Exception:
                    cached = None
                    cached_state = {}
                    latencies = {}
                    qualities = {}
                payload = {
                    "ok": False,
                    "result": None,
                    "error": str(e),
                    "cached": cached,
                    "cached_state": cached_state,
                    "latencies": latencies,
                    "qualities": qualities,
                    "route_apply": "",
                    "route_apply_error": "",
                }

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if generation != self._saved_subscription_load_generation:
                    return
                try:
                    current_state = remote_proxy.load_proxy_subscription_state()
                except Exception:
                    current_state = dict(payload.get("cached_state") or profile)
                    current_state["active_profile_id"] = profile_id
                    if payload["ok"] and payload.get("result") is not None:
                        current_state.update(
                            saved_path=str(payload["result"].saved_path or ""),
                            last_fetched_at=str(payload["result"].last_fetched_at or ""),
                            url=url,
                        )
                if str(current_state.get("active_profile_id") or "") != profile_id:
                    self._refresh_subscription_profile_options(current_state)
                    self._apply_subscription_profile_inputs(current_state)
                    self._set_subscription_nodes(())
                    message = "订阅操作完成时当前分组已切换，未覆盖当前页面。"
                    if not payload["ok"]:
                        message = f"订阅拉取失败，且当前分组已切换: {payload['error']}"
                    elif payload.get("route_apply_error"):
                        message += (
                            " 独立服务线路更新失败，已保留原运行配置: "
                            f"{payload['route_apply_error']}"
                        )
                    elif payload.get("route_apply"):
                        message += f" {payload['route_apply']}"
                    self._set_status(message, "warning")
                    if str(current_state.get("saved_path") or "").strip():
                        self._load_subscription_cache_for_state(current_state, generation)
                    if show_message:
                        show_toast(self.winfo_toplevel(), message, is_error=not payload["ok"])
                    return
                if not payload["ok"]:
                    cached = payload.get("cached")
                    if cached and cached.nodes:
                        cached_state = payload.get("cached_state") or {}
                        self._latency_results = payload.get("latencies") or {}
                        self._quality_results = payload.get("qualities") or {}
                        self._prefer_quality_sort = bool(self._quality_results)
                        selected_key = str(cached_state.get("selected_node_key") or "")
                        self._set_subscription_nodes(cached.nodes, preserve_key=selected_key)
                        self._select_subscription_node_by_key(selected_key)
                        self._use_selected_subscription_node(
                            show_message=False,
                            persist_selection=False,
                        )
                    retain_in_memory = previous_active_id == profile_id and bool(self._subscription_options)
                    if (cached and cached.nodes) or retain_in_memory:
                        prefix = "自动刷新" if auto else "订阅拉取"
                        message = f"{prefix}失败，已保留本机缓存: {payload['error']}"
                        severity = "warning"
                        self._set_cache_status("本机缓存: 拉取失败，继续使用已有节点", "warning")
                    else:
                        self._set_subscription_nodes(())
                        message = f"订阅拉取失败: {payload['error']}"
                        severity = "error"
                        self._set_cache_status("本机缓存: 拉取失败", "error")
                    self._set_status(message, severity)
                    if show_message:
                        show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                result = payload["result"]
                proxy_warning = str(getattr(result, "proxy_warning", "") or "").strip()
                state = current_state
                self._latency_results = remote_proxy.load_proxy_subscription_latencies(state)
                self._quality_results = remote_proxy.load_proxy_subscription_qualities(state)
                self._prefer_quality_sort = bool(self._quality_results)
                selected_key = str(state.get("selected_node_key") or "")
                self._set_subscription_nodes(result.nodes, preserve_key=selected_key)
                if not self._select_subscription_node_by_key(selected_key):
                    self._use_selected_subscription_node(show_message=False, profile_id=profile_id)
                else:
                    self._use_selected_subscription_node(show_message=False, persist_selection=False)
                self._set_cache_status(
                    f"本机缓存: 已保存 {len(result.nodes)} 个节点；刚刚拉取",
                    "success",
                )
                selected = self._subscription_picker.selected_item() if self._subscription_picker else None
                if selected:
                    message = f"订阅已保存到本机缓存；识别到 {len(result.nodes)} 个节点，已填入当前选择。"
                    severity = "success"
                else:
                    message = (
                        f"订阅已保存到本机缓存；识别到 {len(result.nodes)} 个节点。"
                        "没有可自动选择的非香港节点；香港节点仍可手动选择。"
                    )
                    severity = "warning"
                if proxy_warning:
                    message = f"{proxy_warning}；{message}"
                    severity = "warning"
                if payload.get("route_apply_error"):
                    message += (
                        " 独立服务线路更新失败，已保留原运行配置: "
                        f"{payload['route_apply_error']}"
                    )
                    severity = "warning"
                elif payload.get("route_apply"):
                    message += f" {payload['route_apply']}"
                self._set_status(message, severity)
                if show_message:
                    show_toast(self.winfo_toplevel(), message)

            self._run_on_ui_thread(finish)

        threading.Thread(target=run, daemon=True).start()

    def _import_subscription_yaml(self):
        if self._busy or self._periodic_update_running:
            show_toast(self.winfo_toplevel(), "订阅操作正在进行中，请稍后再导入", is_error=True)
            return
        path = filedialog.askopenfilename(
            title="导入本地 Clash/mihomo YAML 节点（不继承 dns/tun）",
            filetypes=[
                ("Clash YAML 配置", "*.yaml *.yml"),
                ("文本配置", "*.txt *.json"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return

        allowed, release_needed = self._begin_subscription_profile_mutation("导入")
        if not allowed:
            return
        self._saved_subscription_load_generation += 1
        generation = self._saved_subscription_load_generation
        self._set_busy(True)
        self._set_cache_status("本机缓存: 正在导入本地 YAML...")
        self._set_status(
            f"正在读取本地 Clash/mihomo 配置并解析全部节点。{LOCAL_YAML_NODE_ONLY_NOTICE}"
        )

        def run():
            try:
                result = remote_proxy.import_proxy_subscription_file(path, activate=True)
                state = remote_proxy.load_proxy_subscription_state()
                payload = {
                    "ok": True,
                    "result": result,
                    "state": state,
                    "profile_id": str(state.get("active_profile_id") or ""),
                    "error": "",
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "result": None,
                    "state": {},
                    "profile_id": "",
                    "error": str(exc),
                }
            finally:
                if release_needed:
                    remote_proxy.release_proxy_subscription_hot_update()

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if generation != self._saved_subscription_load_generation:
                    return
                if not payload["ok"]:
                    message = f"导入本地 Clash YAML 失败: {payload['error']}"
                    self._set_cache_status("本机缓存: 本地 YAML 导入失败，已保留原节点", "error")
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                result = payload["result"]
                state = payload["state"]
                imported_profile_id = str(payload.get("profile_id") or "")
                try:
                    current_state = remote_proxy.load_proxy_subscription_state()
                except Exception:
                    current_state = state
                if str(current_state.get("active_profile_id") or "") != imported_profile_id:
                    self._refresh_subscription_profile_options(current_state)
                    self._apply_subscription_profile_inputs(current_state)
                    self._set_subscription_nodes(())
                    message = (
                        f"已从 {Path(path).name} 导入 {len(result.nodes)} 个节点；"
                        f"当前分组随后发生切换，未覆盖当前页面。{LOCAL_YAML_NODE_ONLY_NOTICE}"
                    )
                    self._set_status(message, "warning")
                    if str(current_state.get("saved_path") or "").strip():
                        self._load_subscription_cache_for_state(current_state, generation)
                    show_toast(self.winfo_toplevel(), message)
                    return
                self._refresh_subscription_profile_options(state)
                self._apply_subscription_profile_inputs(state)
                self._latency_results = remote_proxy.load_proxy_subscription_latencies(state)
                self._quality_results = remote_proxy.load_proxy_subscription_qualities(state)
                self._prefer_quality_sort = bool(self._quality_results)
                selected_key = str(state.get("selected_node_key") or "")
                self._set_subscription_nodes(result.nodes, preserve_key=selected_key)
                if not self._select_subscription_node_by_key(selected_key):
                    self._use_selected_subscription_node(
                        show_message=False,
                        profile_id=imported_profile_id,
                    )
                else:
                    self._use_selected_subscription_node(show_message=False, persist_selection=False)
                self._set_cache_status(
                    f"本机缓存: 已导入 {len(result.nodes)} 个节点；重启后仍可使用",
                    "success",
                )
                selected = self._subscription_picker.selected_item() if self._subscription_picker else None
                if selected and remote_proxy.proxy_subscription_node_is_hong_kong(selected):
                    message = (
                        f"已从 {Path(path).name} 导入 {len(result.nodes)} 个节点并保存到本机缓存；"
                        f"已恢复此前手动选择的香港节点，自动选优仍不会使用香港。{LOCAL_YAML_NODE_ONLY_NOTICE}"
                    )
                    severity = "warning"
                elif selected:
                    message = (
                        f"已从 {Path(path).name} 导入 {len(result.nodes)} 个节点并保存到本机缓存；"
                        f"已填入一个可自动选择的非香港节点。{LOCAL_YAML_NODE_ONLY_NOTICE}"
                    )
                    severity = "success"
                else:
                    message = (
                        f"已从 {Path(path).name} 导入 {len(result.nodes)} 个节点并保存到本机缓存；"
                        f"没有可自动选择的非香港节点，香港节点只保留手动选择。{LOCAL_YAML_NODE_ONLY_NOTICE}"
                    )
                    severity = "warning"
                self._set_status(message, severity)
                show_toast(
                    self.winfo_toplevel(),
                    message,
                    is_error=severity in {"warning", "error"},
                    severity=severity,
                )

            self._run_on_ui_thread(finish)

        try:
            threading.Thread(target=run, name="local-proxy-yaml-import", daemon=True).start()
        except Exception as exc:
            if release_needed:
                remote_proxy.release_proxy_subscription_hot_update()
            self._set_busy(False)
            message = f"启动本地 Clash YAML 导入任务失败: {exc}"
            self._set_cache_status("本机缓存: 本地 YAML 导入未启动", "error")
            self._set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)

    def _measure_subscription_latencies(self):
        if self._busy:
            show_toast(self.winfo_toplevel(), "本机代理操作正在进行中，请稍等", is_error=True)
            return
        if not self._subscription_nodes:
            message = "请先拉取订阅，再测速选择节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        scope_nodes = tuple(self._subscription_batch_nodes())
        scope_label = self._subscription_batch_scope_label()
        if not scope_nodes:
            message = "当前节点分组没有可测速的节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        profile_id = self._current_subscription_profile_id()
        generation = self._saved_subscription_load_generation
        original_selected_key = self._selected_subscription_node_key()
        existing_latency_results = dict(self._latency_results)
        quality_results = dict(self._quality_results)
        tcp_scope_nodes = tuple(
            item
            for item in scope_nodes
            if local_proxy._proxy_node_uses_known_tcp_transport(item.node)
        )
        data_plane_scope_nodes = tuple(
            item
            for item in scope_nodes
            if not local_proxy._proxy_node_uses_known_tcp_transport(item.node)
        )
        self._set_busy(True)
        self._set_status(
            f"正在对 {scope_label} 的 {len(tcp_scope_nodes)} 个 TCP 节点以最多 "
            f"{remote_proxy.PROXY_LATENCY_DEFAULT_MAX_WORKERS} 路并发连续测试 3 次；"
            f"{len(data_plane_scope_nodes)} 个 UDP/其他传输节点直接进入真实数据面验证。"
            "随后用隔离临时 mihomo "
            "做 3 轮 OpenAI API/ChatGPT/Claude/Gemini 稳定验证；"
            "短探针通过后最多深测 5 个候选，每个最坏约 3.2MiB，"
            "用公共上下行传输和官方 compact 无认证路径做 Codex 长会话网络近似；"
            "不使用真实 token，不执行或计费真实 compact；"
            "不会改动系统代理或当前运行节点..."
        )

        def run():
            try:
                results = remote_proxy.measure_proxy_node_latencies(
                    tcp_scope_nodes,
                    timeout=3.0,
                    attempts=3,
                    max_workers=remote_proxy.PROXY_LATENCY_DEFAULT_MAX_WORKERS,
                    require_all=True,
                )
            except Exception as e:
                payload = {
                    "tcp_ok": False,
                    "latencies": {},
                    "stable_node": None,
                    "stability_results": {},
                    "error": str(e),
                }
            else:
                captured_latency_results = dict(existing_latency_results)
                for item in data_plane_scope_nodes:
                    captured_latency_results.pop(
                        remote_proxy.proxy_subscription_node_key(item),
                        None,
                    )
                captured_latency_results.update(results or {})
                save_error = ""
                try:
                    if not profile_id:
                        raise ValueError("当前页面未绑定订阅分组，测速结果未写入其他分组")
                    remote_proxy.save_proxy_subscription_latencies(
                        captured_latency_results,
                        profile_id=profile_id,
                    )
                except Exception as e:
                    save_error = str(e)
                try:
                    stable_node, stability_results = local_proxy.select_stable_local_proxy_node(
                        scope_nodes,
                        results,
                        quality_results,
                        rounds=3,
                    )
                    payload = {
                        "tcp_ok": True,
                        "latencies": results,
                        "captured_latencies": captured_latency_results,
                        "stable_node": stable_node,
                        "stability_results": stability_results,
                        "save_error": save_error,
                        "error": "",
                    }
                except Exception as e:
                    payload = {
                        "tcp_ok": True,
                        "latencies": results,
                        "captured_latencies": captured_latency_results,
                        "stable_node": None,
                        "stability_results": {},
                        "save_error": save_error,
                        "error": str(e),
                    }

            def finish():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if not payload["tcp_ok"]:
                    message = f"节点测速失败: {payload['error']}"
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                captured_latency_results = dict(payload.get("captured_latencies") or {})
                save_error = str(payload.get("save_error") or "")

                current_profile_id = self._current_subscription_profile_id()
                same_context = (
                    generation == self._saved_subscription_load_generation
                    and current_profile_id == profile_id
                )
                if same_context:
                    self._latency_results = captured_latency_results
                    self._prefer_quality_sort = False
                    self._set_subscription_nodes(
                        self._subscription_nodes,
                        preserve_key=original_selected_key,
                    )

                ok_count = sum(
                    1
                    for item in tcp_scope_nodes
                    if remote_proxy.proxy_node_latency_ok(
                        captured_latency_results.get(remote_proxy.proxy_subscription_node_key(item))
                    )
                )
                stable_node = payload.get("stable_node")
                stability_results = payload.get("stability_results") or {}
                stable_key = (
                    remote_proxy.proxy_subscription_node_key(stable_node)
                    if isinstance(stable_node, remote_proxy.ProxySubscriptionNode)
                    else ""
                )
                stable_result = stability_results.get(stable_key)
                verified_count = len(stability_results)
                deep_attempts = int(
                    getattr(stable_result, "deep_transport_attempts", 0) or 0
                )
                deep_successes = int(
                    getattr(stable_result, "deep_transport_successes", 0) or 0
                )
                deep_verified = bool(
                    getattr(stable_result, "deep_transport_ok", False)
                    and deep_attempts == local_proxy.LOCAL_PROXY_DEEP_PROBE_ROUNDS
                    and deep_successes == deep_attempts
                    and getattr(stable_result, "codex_compact_ok", False)
                )
                stable_verified = bool(
                    stable_key
                    and getattr(stable_result, "stable", False)
                    and deep_verified
                    and remote_proxy.proxy_subscription_node_auto_selectable(
                        stable_node,
                        quality_results.get(stable_key),
                    )
                )

                if not same_context:
                    message = (
                        f"原订阅分组的测试已完成：TCP 3/3 通过 "
                        f"{ok_count}/{len(tcp_scope_nodes)} 个，"
                        f"UDP/其他传输 {len(data_plane_scope_nodes)} 个改由实际转发验证；"
                        "页面分组已变更，未覆盖当前选择。"
                    )
                    if save_error:
                        message += f" 测速结果缓存失败: {save_error}"
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                if not stable_verified:
                    message = (
                        f"测试完成: {scope_label} 中 TCP 连续 3 次通过 "
                        f"{ok_count}/{len(tcp_scope_nodes)} 个；"
                        f"UDP/其他传输 {len(data_plane_scope_nodes)} 个跳过 TCP 预检；"
                        f"已验证的 {verified_count} 个非香港候选均未同时通过 3 轮 AI 短探针"
                        "与 Codex 长会话网络近似，"
                        "已保留原选择。"
                        "近似测试不代表真实账号 compact 执行；"
                        "本次测试未改动系统代理或当前运行节点。"
                    )
                    if payload.get("error"):
                        message += f" 稳定验证失败: {payload['error']}"
                    if save_error:
                        message += f" 测速结果缓存失败: {save_error}"
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                if not self._select_subscription_node_by_key(stable_key):
                    message = "稳定节点已验证，但它已不在当前列表中，已保留原选择。"
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return
                self._use_selected_subscription_node(
                    show_message=False,
                    profile_id=profile_id,
                )
                latency = remote_proxy.proxy_node_latency_label(captured_latency_results.get(stable_key))
                region = remote_proxy.proxy_node_region(stable_node.node)
                application_latency = getattr(stable_result, "application_latency_ms", None)
                application_label = (
                    f"OpenAI API 中位 {application_latency}ms"
                    if application_latency is not None
                    else "OpenAI API 三轮通过"
                )
                compact_ok = bool(getattr(stable_result, "codex_compact_ok", False))
                codex_label = (
                    f"Codex 长会话网络近似 {deep_successes}/{deep_attempts} 轮完整传输"
                    + ("，compact 无认证路径预检通过" if compact_ok else "")
                )
                tcp_label = (
                    f"TCP {latency}，"
                    if local_proxy._proxy_node_uses_known_tcp_transport(stable_node.node)
                    else "UDP/其他传输，"
                )
                message = (
                    f"测试完成: TCP 3/3 通过 {ok_count}/{len(tcp_scope_nodes)} 个；"
                    f"UDP/其他传输 {len(data_plane_scope_nodes)} 个改由实际转发验证；"
                    f"已在 {verified_count} 个已验证候选中选择应用时延最低的节点"
                    f"【{region}】{tcp_label}{application_label}，已通过 3 轮 AI 短探针与 {codex_label}。"
                    "这是无 token、无计费的网络近似，不是真实账号 compact。"
                    "验证使用隔离临时 mihomo，未改动系统代理或当前运行节点。"
                )
                severity = "warning" if save_error else "success"
                if save_error:
                    message += f" 测速结果缓存失败: {save_error}"
                self._set_status(message, severity)
                show_toast(self.winfo_toplevel(), message, is_error=bool(save_error))

            self._run_on_ui_thread(finish)

        try:
            threading.Thread(
                target=run,
                name="local-proxy-latency-stability",
                daemon=True,
            ).start()
        except Exception as exc:
            self._set_busy(False)
            message = f"启动节点测速与稳定验证任务失败: {exc}"
            self._set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)

    def _subscription_batch_nodes(self):
        if self._subscription_picker:
            return self._subscription_picker.batch_items()
        return list(self._subscription_nodes)

    def _subscription_batch_scope_label(self) -> str:
        if self._subscription_picker:
            return self._subscription_picker.batch_scope_label()
        return f"全部 {len(self._subscription_nodes)} 个节点"

    def _quality_candidate_nodes(self, nodes=None):
        return list(nodes if nodes is not None else self._subscription_batch_nodes())

    def _subscription_quality_scope(self, region: str = "", nodes=None):
        """Resolve an explicit group action or the picker-wide batch scope."""

        if nodes is not None:
            candidates = list(nodes)
            if not region and candidates:
                region = remote_proxy.proxy_subscription_node_region(candidates[0])
            return candidates, f"{region or '当前'}组全部 {len(candidates)} 个节点"
        if region and self._subscription_picker:
            candidates = self._subscription_picker.group_items(region)
            return candidates, f"{region}组全部 {len(candidates)} 个节点"
        return self._quality_candidate_nodes(), self._subscription_batch_scope_label()

    def _cancel_subscription_quality(self):
        event = self._quality_cancel_event
        if event is None or event.is_set():
            return
        event.set()
        if self._quality_cancel_button:
            self._quality_cancel_button.configure(state="disabled")
        self._set_status("正在停止家宽检测；已开始的 DNS 解析/网络请求结束后即停止...", "warning")

    def _measure_subscription_qualities(self, region: str = "", nodes=None):
        if self._busy:
            show_toast(self.winfo_toplevel(), "本机代理操作正在进行中，请稍等", is_error=True)
            return
        if not self._subscription_nodes:
            message = "请先拉取订阅，再检测节点 IP 质量"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        candidates, scope_label = self._subscription_quality_scope(region, nodes)
        if not candidates:
            message = "当前检测范围没有可检测的节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        node_count = len(candidates)
        settings = network_diagnostic_settings.load_settings()
        configured_services = settings.enabled_services()
        services = remote_proxy.proxy_quality_effective_services(settings, configured_services)
        skipped_services = [service for service in configured_services if service not in services]
        source_label = remote_proxy.quality_source_label_from_settings(settings, services)
        if not services:
            message = "请启用至少一个可执行检测源；Ping0、IPQS、VPNAPI 评估服务器 IP 时需要 API Key"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        skipped_note = ""
        if skipped_services:
            skipped_note = (
                "；已跳过缺 Key 的 "
                + remote_proxy.quality_source_label_from_settings(settings, skipped_services)
            )
        cancel_event = threading.Event()
        self._quality_cancel_event = cancel_event
        self._set_busy(True)
        self._set_status(
            f"正在基于 {source_label} 检测 {scope_label}{skipped_note}；"
            "范围已在启动时锁定，后续筛选或勾选变化不会影响本次任务..."
        )
        candidate_nodes = tuple(candidates)
        existing_quality_results = dict(self._quality_results)
        existing_latency_results = dict(self._latency_results)
        profile_id = self._current_subscription_profile_id()

        def run():
            try:
                progress = {"cached": 0, "complete": 0, "partial": 0, "failed": 0, "reported": 0}

                def report(completed, total, result):
                    result_cancelled = remote_proxy.proxy_node_quality_cancelled(result)
                    if remote_proxy.proxy_node_quality_cached(result) and not result_cancelled:
                        progress["cached"] += 1
                    if remote_proxy.proxy_node_quality_measured(result) and not result_cancelled:
                        bucket = "complete" if remote_proxy.proxy_node_quality_coverage_complete(result) else "partial"
                        progress[bucket] += 1
                    else:
                        progress["failed"] += 1
                    report_step = max(1, total // 20)
                    if completed != total and completed != 1 and completed - progress["reported"] < report_step:
                        return
                    progress["reported"] = completed
                    reused = max(
                        0,
                        completed
                        - progress["cached"]
                        - progress["complete"]
                        - progress["partial"]
                        - progress["failed"],
                    )
                    message = (
                        f"正在检测 {scope_label}: {completed}/{total}；"
                        f"缓存 {progress['cached']}，同 IP 复用 {reused}，完整 {progress['complete']}，"
                        f"部分 {progress['partial']}，失败 {progress['failed']}..."
                    )
                    self._run_on_ui_thread(lambda text=message: self._set_status(text))

                results = remote_proxy.assess_proxy_node_qualities(
                    candidate_nodes,
                    timeout=5.0,
                    max_workers=8,
                    settings=settings,
                    enabled_services=services,
                    progress_callback=report,
                    cancel_event=cancel_event,
                )
                completed_results = {
                    key: value
                    for key, value in (results or {}).items()
                    if remote_proxy.proxy_node_quality_measured(value)
                    and not remote_proxy.proxy_node_quality_cancelled(value)
                }
                merged_results, persisted_results = remote_proxy.merge_proxy_quality_refresh_results(
                    existing_quality_results,
                    completed_results,
                )
                selection_results = {
                    key: merged_results[key]
                    for key in completed_results
                    if key in merged_results
                }
                best = remote_proxy.best_proxy_subscription_node_for_ai_proxy(
                    candidate_nodes,
                    selection_results,
                    existing_latency_results,
                )
                save_error = ""
                try:
                    if not profile_id:
                        raise ValueError("当前页面未绑定订阅分组，质量结果未写入其他分组")
                    remote_proxy.merge_proxy_subscription_qualities(
                        persisted_results,
                        profile_id=profile_id,
                    )
                except Exception as exc:
                    save_error = str(exc)
                payload = {
                    "ok": True,
                    "result": results,
                    "merged_result": merged_results,
                    "best_key": remote_proxy.proxy_node_key(best.node) if best else "",
                    "preserved_complete_count": max(0, len(completed_results) - len(persisted_results)),
                    "save_error": save_error,
                    "cancelled": cancel_event.is_set(),
                    "error": None,
                }
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                if not payload["ok"]:
                    self._quality_cancel_event = None
                    self._set_busy(False)
                    message = f"节点 IP 质量检测失败: {payload['error']}"
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                batch_results = payload["result"] or {}
                cached_count = sum(
                    1
                    for item in batch_results.values()
                    if remote_proxy.proxy_node_quality_cached(item)
                    and not remote_proxy.proxy_node_quality_cancelled(item)
                )
                reused_ip_count = sum(
                    1
                    for item in batch_results.values()
                    if "同一 IP 批次复用" in remote_proxy.proxy_node_quality_detail(item)
                    and not remote_proxy.proxy_node_quality_cancelled(item)
                )
                self._quality_results = dict(payload.get("merged_result") or self._quality_results)
                self._prefer_quality_sort = True
                save_error = str(payload.get("save_error") or "")
                cancelled = bool(payload.get("cancelled"))
                best_key = str(payload.get("best_key") or "")
                preserved_complete_count = int(payload.get("preserved_complete_count") or 0)
                self._set_subscription_nodes(self._subscription_nodes, preserve_key=best_key)
                self._quality_cancel_event = None
                self._set_busy(False)
                tested_count = sum(
                    1
                    for item in candidate_nodes
                    if remote_proxy.proxy_node_quality_measured(
                        batch_results.get(remote_proxy.proxy_subscription_node_key(item))
                    )
                )
                high_count = sum(
                    1
                    for item in candidate_nodes
                    if remote_proxy.proxy_node_quality_for_ai_proxy_ok(
                        batch_results.get(remote_proxy.proxy_subscription_node_key(item))
                    )
                )
                complete_count = sum(
                    1
                    for item in candidate_nodes
                    if remote_proxy.proxy_node_quality_coverage_complete(
                        batch_results.get(remote_proxy.proxy_subscription_node_key(item))
                    )
                )
                partial_count = max(0, tested_count - complete_count)
                failed_count = max(0, node_count - tested_count)
                completion_label = "家宽检测已停止" if cancelled else "家宽检测完成"
                if not best_key:
                    message = (
                        f"{completion_label}: {scope_label}；完整 {complete_count}，"
                        f"部分 {partial_count}，失败/取消 {failed_count}"
                    )
                    if cached_count:
                        message += f"，缓存命中 {cached_count}"
                    if reused_ip_count:
                        message += f"，同 IP 复用 {reused_ip_count}"
                    message += "；暂无可自动选择的非香港高质量结果。香港节点仍可手动选择。"
                    if save_error:
                        message += f" 质量结果缓存失败: {save_error}"
                    self._set_status(message, "warning")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                selected = self._subscription_picker.selected_item() if self._subscription_picker else None
                selected_node = selected.node if selected else {}
                quality = batch_results.get(best_key) or self._quality_results.get(best_key)
                region = remote_proxy.proxy_node_region(selected_node)
                label = remote_proxy.proxy_node_quality_label(quality)
                score = remote_proxy.proxy_node_quality_score(quality)
                basis = remote_proxy.proxy_node_quality_source_label(quality)
                severity = (
                    "success"
                    if remote_proxy.proxy_node_quality_for_ai_proxy_ok(quality)
                    and not save_error
                    else "warning"
                )
                message = (
                    f"{completion_label}: 基于 {basis}，{scope_label}；成功 {tested_count}/{node_count}，"
                    f"完整 {complete_count}，部分 {partial_count}，家宽高质 {high_count}，"
                    f"失败/取消 {failed_count}；已定位服务器 IP 预筛最佳【{region}】{label} 评分{score}。"
                )
                if cached_count:
                    message += f" 缓存命中 {cached_count} 个。"
                if reused_ip_count:
                    message += f" 同 IP 复用 {reused_ip_count} 个。"
                if preserved_complete_count:
                    message += f" 本次 {preserved_complete_count} 个部分结果未覆盖上次完整证据。"
                message += " 检测未切换正在运行的代理；确认后可点击“热更新当前”无重启应用。"
                if save_error:
                    message += f" 质量结果缓存失败: {save_error}"
                self._set_status(message, severity)
                show_toast(self.winfo_toplevel(), message, is_error=bool(save_error))

            self._run_on_ui_thread(finish)

        threading.Thread(target=run, daemon=True).start()

    def _open_proxy_quality_dialog(self):
        top = self.winfo_toplevel()
        if hasattr(top, "_show_proxy_quality_dialog"):
            dialog = top._show_proxy_quality_dialog()
            if dialog is not None:
                self._refresh_subscription_action_hint()
                self._set_status("已打开代理质量检测；可配置检测源和 API Key 池。")
                show_toast(top, "已打开代理质量检测，可选择 Net.Coffee / Ping0 / ProxyCheck / ipapi.is / VPNAPI")
            else:
                self._set_status("代理质量检测窗口打开失败。", "error")
                show_toast(top, "代理质量检测窗口打开失败", is_error=True)
            return
        self._set_status("无法打开代理质量检测窗口。", "error")
        show_toast(top, "无法打开代理质量检测窗口", is_error=True)

    def _measure_selected_subscription_quality(self):
        self._measure_subscription_qualities()

    def _fastest_subscription_node(self, nodes=None):
        return remote_proxy.best_proxy_subscription_node_by_latency(
            nodes if nodes is not None else self._subscription_nodes,
            self._latency_results,
            self._quality_results,
        )

    def _use_selected_subscription_node(
        self,
        show_message: bool = True,
        persist_selection: bool = True,
        profile_id: str = "",
    ):
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
        selection_save_error = ""
        target_profile_id = ""
        if persist_selection:
            target_profile_id = profile_id or self._current_subscription_profile_id()
            if target_profile_id:
                try:
                    remote_proxy.set_proxy_subscription_selected_node(
                        item.node,
                        profile_id=target_profile_id,
                    )
                except Exception as exc:
                    selection_save_error = str(exc)
            else:
                selection_save_error = "当前页面未绑定订阅分组，选择未写入其他分组"
        node_summary = remote_proxy.describe_proxy_node(item.node)
        severity = "warning" if selection_save_error else "success"
        self._set_selected_summary(f"待启动节点: {node_summary}", severity)
        message = f"已填入待启动节点: {node_summary}"
        if selection_save_error:
            message += f"；选择缓存失败: {selection_save_error}"
        self._set_status(message, severity)
        if show_message:
            show_toast(self.winfo_toplevel(), message, is_error=bool(selection_save_error))
            if not selection_save_error and target_profile_id:
                try:
                    bound_services = (
                        local_proxy.local_proxy_service_bindings_for_profile(
                            target_profile_id
                        )
                    )
                except Exception:
                    bound_services = ()
                if bound_services:
                    self._apply_saved_routing(
                        "已保存所选节点，正在同步该订阅绑定的独立服务线路。"
                    )

    def _hot_update_selected_subscription_node(self):
        if self._periodic_update_running:
            message = "订阅定时热更新正在进行中，请完成后再手动热更新当前节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        if self._busy:
            message = "已有代理操作正在进行，请完成后再热更新当前节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        if not self._subscription_picker:
            return
        item = self._subscription_picker.selected_item()
        if not item:
            message = "请先拉取订阅并选择一个节点"
            self._set_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        try:
            proxy_text = remote_proxy.format_proxy_node(item.node)
            node_summary = remote_proxy.describe_proxy_node(item.node)
        except Exception as exc:
            message = f"当前节点格式不正确: {exc}"
            self._set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        candidate_nodes = tuple(self._subscription_nodes)
        quality_results = dict(self._quality_results)
        lock_owned = False
        busy_started = False
        release_guard = threading.Lock()
        lock_released = False

        def release_lock_once():
            nonlocal lock_released
            with release_guard:
                if lock_released:
                    return
                self._subscription_hot_update_lock_owned = False
                remote_proxy.release_proxy_subscription_hot_update()
                lock_released = True

        try:
            if not remote_proxy.try_acquire_proxy_subscription_hot_update():
                message = "另一个订阅热更新正在进行，请稍后再试"
                self._set_status(message, "warning")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return
            lock_owned = True
            self._subscription_hot_update_lock_owned = True

            profile_label = self._subscription_profile_combo.get() if self._subscription_profile_combo else ""
            profile_id = str(self._subscription_profile_options.get(profile_label) or "")
            state = remote_proxy.load_proxy_subscription_state()
            profiles = state.get("profiles") if isinstance(state.get("profiles"), dict) else {}
            if not profile_id:
                profile_id = str(state.get("active_profile_id") or "")
            if not profile_id or not isinstance(profiles.get(profile_id), dict):
                raise RuntimeError("当前订阅分组不存在，请重新选择后再试")
            generation = self._saved_subscription_load_generation

            busy_started = True
            self._set_busy(True)
            self._set_status(
                f"正在将当前节点【{node_summary}】无重启热更新到运行中的本机 AI 代理，并验证连通性；"
                "若代理未运行将直接跳过，不会自动启动或部署..."
            )

            def run():
                try:
                    payload = {
                        "ok": True,
                        "result": local_proxy.reload_local_ai_proxy_verified(
                            proxy_text,
                            candidate_nodes,
                            quality_results=quality_results,
                            profile_id=profile_id,
                        ),
                        "error": "",
                    }
                except Exception as exc:
                    payload = {"ok": False, "result": None, "error": str(exc)}
                finally:
                    release_lock_once()

                def finish():
                    if not self.winfo_exists():
                        return
                    self._set_busy(False)
                    if not payload["ok"]:
                        message = f"热更新当前节点失败: {payload['error']}"
                        self._set_status(message, "error")
                        show_toast(self.winfo_toplevel(), message, is_error=True)
                        return

                    if generation == self._saved_subscription_load_generation:
                        current_label = (
                            self._subscription_profile_combo.get()
                            if self._subscription_profile_combo
                            else ""
                        )
                        current_profile_id = str(
                            self._subscription_profile_options.get(current_label) or ""
                        )
                        if current_profile_id == profile_id:
                            try:
                                node_key = local_proxy.current_local_ai_proxy_node_key()
                                if node_key and self._select_subscription_node_by_key(node_key):
                                    self._use_selected_subscription_node(
                                        show_message=False,
                                        persist_selection=False,
                                    )
                            except Exception:
                                pass

                    message = str(payload["result"])
                    severity = self._hot_update_message_severity(message)
                    self._set_status(message, severity)
                    show_toast(
                        self.winfo_toplevel(),
                        message,
                        is_error=severity in {"warning", "error"},
                        severity=severity,
                    )

                self._run_on_ui_thread(finish)

            threading.Thread(
                target=run,
                name="local-proxy-selected-node-hot-update",
                daemon=True,
            ).start()
            lock_owned = False
        except Exception as exc:
            if busy_started:
                self._set_busy(False)
            message = f"热更新当前节点启动失败: {exc}"
            self._set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
        finally:
            if lock_owned:
                release_lock_once()

    @staticmethod
    def _hot_update_message_severity(message: str) -> str:
        text = str(message or "")
        if "验证通过" in text and not any(marker in text for marker in ("未完全", "失败", "跳过", "不可用")):
            return "success"
        return "warning"

    def _node_input(self) -> str:
        if self._node_text:
            text = self._node_text.get("1.0", "end").strip()
            if text:
                return text
        item = self._selected_subscription_item()
        if not item:
            return ""
        try:
            return remote_proxy.format_proxy_node(item.node)
        except Exception:
            return ""

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
            content = remote_proxy.read_proxy_node_text_file(path)
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

    def _run_local_task(
        self,
        busy_message: str,
        worker,
        success_prefix: str,
        on_success=None,
        severity_from_result=None,
        on_error=None,
        failure_hint: str = "",
    ):
        if self._busy:
            show_toast(self.winfo_toplevel(), "本机代理操作正在进行中，请稍等", is_error=True)
            return
        self._set_busy(True)
        self._set_status(busy_message, "busy")

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
                    if on_error:
                        try:
                            on_error(payload["error"])
                        except Exception:
                            pass
                    message = f"{success_prefix}失败: {payload['error']}"
                    if failure_hint:
                        message = f"{message}；{failure_hint}"
                    self._set_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return
                if on_success:
                    try:
                        on_success(payload["result"])
                    except Exception:
                        pass
                message = str(payload["result"])
                severity = (
                    severity_from_result(message)
                    if severity_from_result
                    else infer_feedback_severity(message)
                )
                self._set_status(message, severity)
                show_toast(
                    self.winfo_toplevel(),
                    message,
                    is_error=severity in {"warning", "error"},
                    severity=severity,
                )

            self._run_on_ui_thread(finish)

        try:
            threading.Thread(target=run, daemon=True).start()
        except Exception as exc:
            self._set_busy(False)
            message = f"{success_prefix}启动失败: {exc}"
            if failure_hint:
                message = f"{message}；{failure_hint}"
            self._set_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)

    def _start_local_proxy(self):
        proxy_text = self._node_input()
        strict_privacy = self._strict_privacy_selected()
        wsl_sharing = self._wsl_share_selected()
        service_binding_count = len(self._service_route_bindings)
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
                "正在启动 Windows 本机 AI 代理，优先验证 Codex/OpenAI 并等待内核故障切换...",
                lambda: local_proxy.install_local_ai_proxy_verified(
                    proxy_text,
                    tuple(self._subscription_nodes),
                    quality_results=self._quality_results,
                ),
                "启动本机 AI 代理",
                on_success=sync_started_node,
                severity_from_result=lambda message: "error"
                if "恢复原节点失败" in message
                else "warning"
                if any(
                    marker in message
                    for marker in (
                        "启动验证失败",
                        "验证执行失败",
                        "自动回滚未完全完成",
                        "验证未完全通过",
                        "其他 AI 服务未完全可达",
                        "自动尝试",
                        "已恢复原节点",
                        "已跳过",
                    )
                )
                else infer_feedback_severity(message),
                failure_hint="启动未完成；程序已执行事务回滚保护，请点“检查状态”确认当前代理",
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="启动 Win11 本机代理",
            message=(
                "将使用当前节点启动 Windows 本机 mihomo，并写入当前 Windows 用户的 "
                "HTTP_PROXY/HTTPS_PROXY/ALL_PROXY、VS Code 本机代理设置，以及 Win11 当前用户系统代理。\n"
                f"识别到节点: {node_summary}\n"
                + (
                    "严格隐私已开启：已进入 mihomo 的公网流量不 DIRECT，订阅代理失败不直连回退。\n"
                    if strict_privacy
                    else "严格隐私未开启：未命中代理规则的流量使用系统 DNS 并可能 DIRECT。\n"
                )
                + "如果当前订阅有合格候选，会一并装载最多 4 个备用节点，主节点失效后由 mihomo 为新连接自动切换。\n"
                + (
                    f"已配置 {service_binding_count} 项服务订阅绑定；会从各自订阅缓存装载独立节点池，"
                    "不会切换订阅页面或改写 Codex/Claude 登录状态。\n"
                    if service_binding_count
                    else "服务线路均跟随当前节点，保持原有单订阅行为。\n"
                )
                + (
                    "WSL 共享已开启：会识别默认发行版的实际网络模式；镜像网络只走回环，"
                    "NAT 只对白名单虚拟子网开放，并写入可撤销的 shell/VS Code Remote 环境入口。\n"
                    if wsl_sharing
                    else "WSL 共享未开启：本次不会修改 WSL 环境或监听边界。\n"
                )
                + "启动只执行有上限的快速验证；耗时的逐节点长会话深测请在节点页手动执行。\n"
                + "启动时会并行比较普通网站的原生直连与代理后连通性；"
                "只有确认启用代理造成退化时才自动回滚。\n"
                + "这不是 VPN/TUN，无法阻止忽略代理的程序、WebRTC/UDP 或系统 DNS/IPv6 绕过。"
                "启动后已打开的 Codex/Claude Code 不会自动继承新环境，"
                "请完全退出后重开 Codex、Claude Code、VS Code 和终端。"
            ),
            on_confirm=do_start,
        )

    def _inspect_local_proxy(self):
        def inspect():
            summary = local_proxy.inspect_local_ai_proxy().summary()
            return (
                f"{summary}\n严格隐私状态来自当前运行的受管 YAML，不是界面开关的声明值。"
                "不是 VPN/TUN；状态正常也不代表已阻止系统 DNS、"
                "WebRTC/UDP 或忽略代理的程序绕过。"
            )

        self._run_local_task(
            "正在检查 Windows 本机 AI 代理状态...",
            inspect,
            "检查本机 AI 代理",
            severity_from_result=lambda message: "warning"
            if any(marker in message for marker in ("未运行", "未指向", "健康检查失败", "漂移", "未确认"))
            else "success",
            failure_hint="本次仅执行状态检查，未修改本机代理设置",
        )

    def _inspect_and_cleanup_stale_proxy(self):
        """Inspect stale loopback settings and request explicit cleanup."""

        from core.api_tester import APITester

        def confirm_cleanup(inspection):
            if inspection.protected_message or not inspection.has_invalid_settings:
                return
            details = inspection.confirmation_details()
            reconciliation = (
                f"{inspection.reconciliation_message}\n\n"
                if inspection.reconciliation_message
                else ""
            )
            ConfirmDialog(
                self.winfo_toplevel(),
                title="清理脏代理设置",
                message=(
                    reconciliation
                    + "检测到以下设置仍指向拒绝连接的本机回环端口：\n"
                    + details
                    + "\n\n确认后会再次检查端口和设置值，只清理仍与上述清单一致的项目。"
                    "正在工作的本机代理、远程代理、NO_PROXY、PAC、自动检测和其他 VS Code 设置不会被修改。"
                    "Windows 系统代理仅关闭 ProxyEnable，原 ProxyServer 保留，方便代理软件以后重新启用。"
                ),
                on_confirm=lambda: self._perform_stale_proxy_cleanup(inspection),
            )

        self._run_local_task(
            "正在检查环境变量、Win11 系统代理和 VS Code 中的脏代理残留...",
            APITester.inspect_invalid_proxy_settings_for_cleanup,
            "检查脏代理设置",
            on_success=confirm_cleanup,
            severity_from_result=lambda message: "error"
            if any(marker in message for marker in ("失败", "未完成"))
            else "warning"
            if any(
                marker in message
                for marker in (
                    "检测到",
                    "等待用户确认",
                    "未自动改动",
                    "保护期",
                    "未清理",
                    "仅支持 Windows",
                )
            )
            else "success",
            failure_hint="本次检查失败，未修改任何代理设置",
        )

    def _perform_stale_proxy_cleanup(self, inspection):
        from core.api_tester import APITester

        self._run_local_task(
            "正在复核并清理确认过的失效回环代理设置...",
            lambda: APITester.clear_invalid_proxy_settings(inspection),
            "清理脏代理设置",
            severity_from_result=lambda message: "error"
            if "清理失败" in message
            else "warning"
            if "未清理任何项目" in message
            else "success",
            failure_hint="清理未完成；未通过二次校验的设置保持原样",
        )

    def _update_local_proxy_core(self):
        self._run_local_task(
            "正在检查、下载并校验 mihomo 官方稳定版；当前代理不会自动重启...",
            local_proxy.update_local_mihomo_core,
            "更新 mihomo 内核",
            severity_from_result=lambda message: (
                "warning" if any(marker in message for marker in ("失败", "待代理重启")) else "success"
            ),
            failure_hint="内核更新失败不会替换现有可用版本，也不会修改代理环境或当前节点",
        )

    def _probe_local_proxy(self):
        def probe():
            result = local_proxy.probe_local_ai_proxy()
            return f"{result}\n此检测只证明明确经过 mihomo 的请求可用，不是 DNS/真实 IP 泄露证明。"

        self._run_local_task(
            "正在并行测试 OpenAI API、ChatGPT、Claude 和 Gemini 连通性...",
            probe,
            "测试本机 AI 代理",
            severity_from_result=lambda message: (
                "success" if remote_proxy._probe_summary_all_ok(message) else "warning"
            ),
            failure_hint="本次仅执行连通性测试，未修改本机代理设置",
        )

    def _stop_local_proxy(self):
        def do_stop():
            self._run_local_task(
                "正在停止 Windows 本机 AI 代理并恢复本工具写入的代理环境...",
                local_proxy.stop_local_ai_proxy,
                "停止本机 AI 代理",
                severity_from_result=lambda message: "error"
                if "恢复设置失败" in message
                else "warning"
                if "未停止" in message
                else "success",
                failure_hint="停止或恢复未完成，请点“检查状态”核对环境变量、系统代理和进程",
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="停止 Win11 本机代理",
            message=(
                "将停止本工具启动的本机 mihomo，并尽量恢复启动前的 Windows 用户代理环境变量、"
                "Win11 系统代理和 VS Code 代理设置；同时删除本工具写入的 WSL shell/VS Code Remote "
                "环境入口及受管防火墙规则。"
            ),
            on_confirm=do_stop,
        )
