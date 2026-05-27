import threading
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast
from ui.dialogs.ssh_editor import SSHEditorDialog
from ui.dialogs.confirm_dialog import ConfirmDialog
from core import (
    profile_manager,
    remote_auto_continue,
    remote_git_login,
    remote_proxy,
    ssh_manager,
    sync_manager,
)
from core.auto_continue.manager import auto_continue_manager
from models.auto_continue import training_prompt_template_by_key
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font, input_style, textbox_style


def _format_server_batch_item(server_name: str, result) -> str:
    text = str(result or "操作完成")
    if text.startswith(f"{server_name}:") or text.startswith(f"{server_name}："):
        return text
    return f"{server_name}: {text}"


class SSHTab(ctk.CTkScrollableFrame):
    """Tab for managing SSH servers and syncing configs."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.configure(fg_color="transparent")
        self._cards_frame = None
        self._sync_frame = None
        self._sync_kind_combo = None
        self._profile_combo = None
        self._remote_pull_type_combo = None
        self._remote_pull_combo = None
        self._remote_pull_hint = None
        self._remote_inspect_button = None
        self._remote_pull_button = None
        self._codex_wire_api_combo = None
        self._codex_wire_api_hint = None
        self._clear_api_combo = None
        self._server_combo_label = None
        self._target_summary_label = None
        self._target_hint_label = None
        self._sync_current_button = None
        self._sync_selected_button = None
        self._git_login_status_label = None
        self._sync_status_label = None
        self._ssh_busy = False
        self._remote_config_candidates = []
        self._remote_pull_options = {}
        self._remote_pull_all_label = "全部可拉取配置"
        self._remote_pull_server_name = None
        self._selected_server_names: set[str] = set()
        self._batch_target_label = None
        self._batch_select_all_button = None
        self._batch_clear_button = None
        self._proxy_subscription_entry = None
        self._proxy_subscription_combo = None
        self._proxy_fetch_button = None
        self._proxy_use_node_button = None
        self._proxy_latency_button = None
        self._proxy_auto_refresh_var = ctk.BooleanVar(value=False)
        self._proxy_auto_refresh_check = None
        self._proxy_periodic_update_var = ctk.BooleanVar(value=False)
        self._proxy_periodic_update_check = None
        self._proxy_periodic_update_entry = None
        self._proxy_periodic_update_after_id = None
        self._proxy_periodic_update_running = False
        self._proxy_startup_refresh_after_id = None
        self._proxy_subscription_nodes = []
        self._proxy_subscription_options = {}
        self._proxy_latency_results = {}
        self._proxy_latency_server_count = 0
        self._proxy_busy = False
        self._proxy_saved_subscription_loaded = False
        self._proxy_node_text = None
        self._proxy_target_label = None
        self._proxy_cache_label = None
        self._proxy_selected_label = None
        self._proxy_load_file_button = None
        self._proxy_deploy_button = None
        self._proxy_inspect_button = None
        self._proxy_remote_test_button = None
        self._proxy_remote_cleanup_button = None
        self._proxy_status_label = None
        self._remote_pull_type_options = {
            "全部项目": "all",
            "仅 API": "api",
            "仅账号": "account",
            "仅 Claude": "claude",
            "仅 Codex": "codex",
        }
        self._remote_auto_provider_combo = None
        self._remote_auto_feature_label = None
        self._remote_auto_status_label = None
        self._remote_auto_buttons = []
        self._remote_auto_switches = []
        self._remote_auto_refreshing = False
        self._remote_auto_continue_var = ctk.BooleanVar(value=False)
        self._remote_git_snapshot_var = ctk.BooleanVar(value=True)
        self._remote_git_snapshot_on_start_var = ctk.BooleanVar(value=True)
        self._remote_git_snapshot_on_recovery_var = ctk.BooleanVar(value=True)
        self._remote_git_auto_push_var = ctk.BooleanVar(value=False)
        self._remote_training_auto_continue_var = ctk.BooleanVar(value=False)
        self._remote_error_recovery_var = ctk.BooleanVar(value=False)
        self._remote_permission_auto_approve_var = ctk.BooleanVar(value=False)
        self._remote_permission_auto_approve_switch = None
        self._remote_git_snapshot_on_start_switch = None
        self._remote_git_snapshot_on_recovery_switch = None
        self._remote_git_auto_push_switch = None
        self._remote_auto_last_statuses = {}
        self._remote_auto_last_payload = None
        self._remote_auto_busy = False
        self._sync_kind_options = {
            "Claude API": "claude_api",
            "Claude 账号": "claude_account",
            "Codex API": "codex_api",
            "Codex 账号": "codex_account",
        }
        self._remote_auto_options = {
            "Claude": "claude",
            "Codex": "codex",
            "Claude + Codex": "all",
        }
        self._codex_wire_api_options = {
            "远端自测选择": "auto",
            "使用本地配置": "profile",
            "强制 responses": "responses",
        }
        self._clear_api_options = {
            "Claude + Codex": "all",
            "Claude API": "claude",
            "Codex API": "codex",
        }
        self._build_ui()

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(14, 8))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="SSH 服务器管理",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="连接远程服务器，双向同步 API、账号、Git 登录和 Hook 设置",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header,
            text="+ 新建服务器",
            width=126,
            command=self._create_server,
            **button_style("primary"),
        ).pack(side="right")

        # Server cards
        self._cards_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._cards_frame.pack(fill="x", padx=14, pady=(0, 8))

        # Sync panel
        sync_header = ctk.CTkFrame(self, fg_color="transparent")
        sync_header.pack(fill="x", padx=14, pady=(8, 5))
        ctk.CTkLabel(
            sync_header,
            text="配置同步",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(side="left")
        self._batch_target_label = ctk.CTkLabel(
            sync_header,
            text="远端批量: 未勾选（单台模式）",
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._batch_target_label.pack(side="left", padx=(12, 0))
        ctk.CTkFrame(sync_header, fg_color="transparent").pack(side="left", fill="x", expand=True)
        self._batch_select_all_button = ctk.CTkButton(
            sync_header,
            text="全选远端",
            width=86,
            command=self._select_all_batch_servers,
            **button_style("secondary", compact=True),
        )
        self._batch_select_all_button.pack(side="right", padx=(6, 0))
        self._batch_clear_button = ctk.CTkButton(
            sync_header,
            text="清空批量",
            width=78,
            command=self._clear_batch_servers,
            **button_style("secondary", compact=True),
        )
        self._batch_clear_button.pack(side="right")

        self._sync_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        self._sync_frame.pack(fill="x", padx=14, pady=(0, 12))

        # Sync controls
        sync_controls = ctk.CTkFrame(self._sync_frame, fg_color="transparent")
        sync_controls.pack(fill="x", padx=14, pady=14)
        sync_controls.grid_columnconfigure(1, weight=1)
        sync_controls.grid_columnconfigure(2, weight=1)

        # Target selector
        self._server_combo_label = ctk.CTkLabel(
            sync_controls,
            text="单台目标",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        )
        self._server_combo_label.grid(row=0, column=0, sticky="w")
        self._server_combo = ctk.CTkComboBox(
            sync_controls,
            values=["(无)"],
            width=220,
            command=lambda _value: self._on_server_selection_change(),
            **combo_style(),
        )
        self._server_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12))
        self._target_summary_label = ctk.CTkLabel(
            sync_controls,
            text="单台模式",
            text_color=COLORS["muted"],
            font=font(12, "bold"),
            anchor="w",
            justify="left",
        )
        self._target_summary_label.grid(row=0, column=2, columnspan=2, sticky="ew")
        self._target_hint_label = ctk.CTkLabel(
            sync_controls,
            text="单台模式下，推送/清理/Git 同步/SSH AI 代理使用单台目标；勾选服务器卡片后这些操作改为远端批量。远端拉取始终只读取单台目标。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._target_hint_label.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        bind_wraplength(sync_controls, self._target_hint_label, padding=20)

        ctk.CTkLabel(
            sync_controls,
            text="推送内容",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(12, 0))
        self._sync_kind_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._sync_kind_options.keys()),
            width=132,
            command=lambda _value: self._on_sync_kind_change(),
            **combo_style(),
        )
        self._sync_kind_combo.grid(row=2, column=1, sticky="w", padx=(8, 12), pady=(12, 0))
        self._sync_kind_combo.set("Claude API")

        self._profile_combo = ctk.CTkComboBox(
            sync_controls,
            values=["(无)"],
            width=220,
            **combo_style(),
        )
        self._profile_combo.grid(row=2, column=2, sticky="ew", padx=(0, 8), pady=(12, 0))

        push_button_frame = ctk.CTkFrame(sync_controls, fg_color="transparent")
        push_button_frame.grid(row=2, column=3, sticky="e", pady=(12, 0))
        self._sync_current_button = ctk.CTkButton(
            push_button_frame,
            text="推送当前",
            width=118,
            command=self._sync_current,
            **button_style("primary", compact=True),
        )
        self._sync_current_button.pack(side="left", padx=(0, 6))
        self._sync_selected_button = ctk.CTkButton(
            push_button_frame,
            text="推送所选",
            width=118,
            command=self._sync_selected,
            **button_style("primary", compact=True),
        )
        self._sync_selected_button.pack(side="left")

        ctk.CTkLabel(
            sync_controls,
            text="远端拉取",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=3, column=0, sticky="w", pady=(10, 0))
        self._remote_pull_type_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._remote_pull_type_options.keys()),
            width=132,
            command=lambda _value: self._refresh_remote_pull_combo(),
            state="disabled",
            **combo_style(),
        )
        self._remote_pull_type_combo.grid(row=3, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._remote_pull_type_combo.set("全部项目")
        self._remote_pull_combo = ctk.CTkComboBox(
            sync_controls,
            values=["请先读取远端配置"],
            width=220,
            state="disabled",
            **combo_style(),
        )
        self._remote_pull_combo.grid(row=3, column=2, sticky="ew", padx=(0, 8), pady=(10, 0))
        self._remote_pull_combo.set("请先读取远端配置")
        remote_pull_button_frame = ctk.CTkFrame(sync_controls, fg_color="transparent")
        remote_pull_button_frame.grid(row=3, column=3, sticky="e", pady=(10, 0))
        self._remote_inspect_button = ctk.CTkButton(
            remote_pull_button_frame,
            text="读取单台",
            width=86,
            command=self._inspect_remote_configs,
            **button_style("secondary", compact=True),
        )
        self._remote_inspect_button.pack(side="left", padx=(0, 6))
        self._remote_pull_button = ctk.CTkButton(
            remote_pull_button_frame,
            text="拉取到本机",
            width=86,
            command=self._pull_from_server,
            state="disabled",
            **button_style("accent", compact=True),
        )
        self._remote_pull_button.pack(side="left")
        self._remote_pull_hint = ctk.CTkLabel(
            sync_controls,
            text="远端拉取只读取单台目标，不受批量勾选影响；先读取实际存在的配置，再按 API/账号或 Claude/Codex 过滤。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._remote_pull_hint.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        bind_wraplength(sync_controls, self._remote_pull_hint, padding=20)

        ctk.CTkLabel(
            sync_controls,
            text="Codex Wire API",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=5, column=0, sticky="w", pady=(10, 0))
        self._codex_wire_api_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._codex_wire_api_options.keys()),
            width=160,
            **combo_style(),
        )
        self._codex_wire_api_combo.grid(row=5, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._codex_wire_api_combo.set("远端自测选择")
        self._codex_wire_api_hint = ctk.CTkLabel(
            sync_controls,
            text="推送 Codex API 时生效；远端自测会在服务器上各跑 3 次并回写最稳选项",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._codex_wire_api_hint.grid(row=5, column=2, columnspan=2, sticky="ew", pady=(10, 0))
        bind_wraplength(sync_controls, self._codex_wire_api_hint, padding=20)

        ctk.CTkLabel(
            sync_controls,
            text="远端清理",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=6, column=0, sticky="w", pady=(10, 0))
        self._clear_api_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._clear_api_options.keys()),
            width=160,
            **combo_style(),
        )
        self._clear_api_combo.grid(row=6, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._clear_api_combo.set("Claude + Codex")
        clear_hint = ctk.CTkLabel(
            sync_controls,
            text="移除服务器当前 API Key/Token、Base URL 覆盖和本工具写入的相关远端环境变量。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        clear_hint.grid(row=6, column=2, sticky="ew", pady=(10, 0))
        bind_wraplength(sync_controls, clear_hint, padding=20)
        ctk.CTkButton(
            sync_controls,
            text="清除远端 API",
            width=126,
            command=self._clear_remote_api_info,
            **button_style("danger"),
        ).grid(row=6, column=3, sticky="e", pady=(10, 0))

        ctk.CTkLabel(
            sync_controls,
            text="Git 登录",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=7, column=0, sticky="w", pady=(10, 0))
        self._git_login_status_label = ctk.CTkLabel(
            sync_controls,
            text="检查/从 SSH 导入只使用单台目标；同步到 SSH 使用远端批量目标，未勾选时使用单台目标。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._git_login_status_label.grid(row=7, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(10, 0))
        bind_wraplength(sync_controls, self._git_login_status_label, padding=20)
        git_btn_frame = ctk.CTkFrame(sync_controls, fg_color="transparent")
        git_btn_frame.grid(row=7, column=3, sticky="e", pady=(10, 0))
        ctk.CTkButton(
            git_btn_frame,
            text="检查",
            width=58,
            command=self._inspect_git_login,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            git_btn_frame,
            text="同步到 SSH",
            width=84,
            command=self._sync_git_login,
            **button_style("accent", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            git_btn_frame,
            text="从 SSH 导入",
            width=86,
            command=self._import_git_login,
            **button_style("secondary", compact=True),
        ).pack(side="left")

        self._sync_status_label = ctk.CTkLabel(
            sync_controls,
            text="就绪",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._sync_status_label.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        bind_wraplength(sync_controls, self._sync_status_label, padding=20)

        proxy_header = ctk.CTkFrame(self, fg_color="transparent")
        proxy_header.pack(fill="x", padx=14, pady=(4, 5))
        ctk.CTkLabel(
            proxy_header,
            text="SSH 远端 AI 代理",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            proxy_header,
            text="写入 VS Code Remote/Codex/Claude Code 的远端代理入口；Win11 本机代理已移到单独标签页",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left", padx=(10, 0))
        ctk.CTkFrame(proxy_header, fg_color="transparent").pack(side="left", fill="x", expand=True)
        self._proxy_target_label = ctk.CTkLabel(
            proxy_header,
            text="SSH 目标: 未选择服务器",
            text_color=COLORS["warning"],
            font=font(12, "bold"),
        )
        self._proxy_target_label.pack(side="right")

        proxy_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        proxy_frame.pack(fill="x", padx=14, pady=(0, 12))
        proxy_controls = ctk.CTkFrame(proxy_frame, fg_color="transparent")
        proxy_controls.pack(fill="x", padx=14, pady=14)
        proxy_controls.grid_columnconfigure(1, weight=1)
        proxy_controls.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(
            proxy_controls,
            text="1 订阅来源",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        ).grid(row=0, column=0, columnspan=4, sticky="ew")
        ctk.CTkLabel(
            proxy_controls,
            text="订阅链接",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self._proxy_subscription_entry = ctk.CTkEntry(
            proxy_controls,
            placeholder_text="粘贴 Clash/mihomo 订阅链接；只保存在本机缓存，不写入远端",
            **input_style(),
        )
        self._proxy_subscription_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        proxy_sub_action_frame = ctk.CTkFrame(proxy_controls, fg_color="transparent")
        proxy_sub_action_frame.grid(row=1, column=3, sticky="e", pady=(8, 0))
        self._proxy_fetch_button = ctk.CTkButton(
            proxy_sub_action_frame,
            text="拉取订阅",
            width=86,
            command=self._fetch_proxy_subscription,
            **button_style("secondary", compact=True),
        )
        self._proxy_fetch_button.pack(side="left", padx=(0, 6))
        self._proxy_auto_refresh_check = ctk.CTkCheckBox(
            proxy_sub_action_frame,
            text="启动时刷新",
            width=84,
            checkbox_width=16,
            checkbox_height=16,
            variable=self._proxy_auto_refresh_var,
            command=self._on_proxy_auto_refresh_toggle,
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._proxy_auto_refresh_check.pack(side="left")
        self._proxy_periodic_update_check = ctk.CTkCheckBox(
            proxy_sub_action_frame,
            text="定时热更新",
            width=96,
            checkbox_width=16,
            checkbox_height=16,
            variable=self._proxy_periodic_update_var,
            command=self._on_proxy_periodic_update_toggle,
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._proxy_periodic_update_check.pack(side="left", padx=(8, 0))
        self._proxy_periodic_update_entry = ctk.CTkEntry(
            proxy_sub_action_frame,
            width=48,
            placeholder_text="60",
            **input_style(),
        )
        self._proxy_periodic_update_entry.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            proxy_sub_action_frame,
            text="分钟",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left", padx=(4, 0))
        self._proxy_cache_label = ctk.CTkLabel(
            proxy_controls,
            text="本机缓存: 未加载",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._proxy_cache_label.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        bind_wraplength(proxy_controls, self._proxy_cache_label, padding=20)

        ctk.CTkLabel(
            proxy_controls,
            text="2 节点选择",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        ).grid(row=3, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        ctk.CTkLabel(
            proxy_controls,
            text="订阅节点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))
        self._proxy_subscription_combo = ctk.CTkComboBox(
            proxy_controls,
            values=["请先拉取订阅"],
            state="disabled",
            **combo_style(),
        )
        self._proxy_subscription_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))
        self._proxy_subscription_combo.set("请先拉取订阅")
        proxy_node_actions = ctk.CTkFrame(proxy_controls, fg_color="transparent")
        proxy_node_actions.grid(row=4, column=3, sticky="e", pady=(8, 0))
        self._proxy_latency_button = ctk.CTkButton(
            proxy_node_actions,
            text="测速选最快",
            width=98,
            command=self._measure_proxy_subscription_latencies,
            state="disabled",
            **button_style("secondary", compact=True),
        )
        self._proxy_latency_button.pack(anchor="e", pady=(0, 6))
        self._proxy_use_node_button = ctk.CTkButton(
            proxy_node_actions,
            text="填入待部署",
            width=98,
            command=self._use_selected_proxy_subscription_node,
            state="disabled",
            **button_style("accent", compact=True),
        )
        self._proxy_use_node_button.pack(anchor="e")
        self._proxy_selected_label = ctk.CTkLabel(
            proxy_controls,
            text="待部署节点: 未选择",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._proxy_selected_label.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
        bind_wraplength(proxy_controls, self._proxy_selected_label, padding=20)

        ctk.CTkLabel(
            proxy_controls,
            text="3 应用到目标",
            text_color=COLORS["text"],
            font=font(13, "bold"),
            anchor="w",
        ).grid(row=6, column=0, columnspan=4, sticky="ew", pady=(14, 0))
        ctk.CTkLabel(
            proxy_controls,
            text="待部署节点",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=7, column=0, sticky="nw", pady=(8, 0))
        self._proxy_node_text = ctk.CTkTextbox(
            proxy_controls,
            height=96,
            **textbox_style(monospace=True),
        )
        self._proxy_node_text.grid(row=7, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(8, 0))

        proxy_button_frame = ctk.CTkFrame(proxy_controls, fg_color="transparent")
        proxy_button_frame.grid(row=7, column=3, sticky="ne", pady=(8, 0))
        ctk.CTkLabel(
            proxy_button_frame,
            text="节点来源",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(0, 4))
        self._proxy_load_file_button = ctk.CTkButton(
            proxy_button_frame,
            text="导入文件",
            width=96,
            command=self._load_proxy_node_file,
            **button_style("secondary", compact=True),
        )
        self._proxy_load_file_button.pack(anchor="e", pady=(0, 10))
        ctk.CTkLabel(
            proxy_button_frame,
            text="SSH 远端",
            text_color=COLORS["muted"],
            font=font(11, "bold"),
            anchor="e",
        ).pack(anchor="e", pady=(0, 4))
        self._proxy_deploy_button = ctk.CTkButton(
            proxy_button_frame,
            text="部署远端",
            width=96,
            command=self._deploy_ai_proxy,
            **button_style("accent", compact=True),
        )
        self._proxy_deploy_button.pack(anchor="e", pady=(0, 6))
        self._proxy_inspect_button = ctk.CTkButton(
            proxy_button_frame,
            text="检查远端",
            width=96,
            command=self._inspect_ai_proxy,
            **button_style("secondary", compact=True),
        )
        self._proxy_inspect_button.pack(anchor="e", pady=(0, 6))
        self._proxy_remote_test_button = ctk.CTkButton(
            proxy_button_frame,
            text="测试远端",
            width=96,
            command=self._probe_ai_proxy,
            **button_style("secondary", compact=True),
        )
        self._proxy_remote_test_button.pack(anchor="e", pady=(0, 6))
        self._proxy_remote_cleanup_button = ctk.CTkButton(
            proxy_button_frame,
            text="清理远端",
            width=96,
            command=self._cleanup_ai_proxy,
            **button_style("danger", compact=True),
        )
        self._proxy_remote_cleanup_button.pack(anchor="e", pady=(0, 10))

        self._proxy_status_label = ctk.CTkLabel(
            proxy_controls,
            text="本页只影响 SSH 服务器；部署远端使用 SSH 目标（远端批量优先）。Win11 本机代理请使用“Win11 代理”标签页。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._proxy_status_label.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        bind_wraplength(proxy_controls, self._proxy_status_label, padding=20)
        self._update_proxy_target_label()

        auto_header = ctk.CTkFrame(self, fg_color="transparent")
        auto_header.pack(fill="x", padx=14, pady=(4, 5))
        ctk.CTkLabel(
            auto_header,
            text="远端自动续跑",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            auto_header,
            text="同步 Stop 续跑、训练守护、Git 快照、API 恢复和权限自动确认到 SSH",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left", padx=(10, 0))

        auto_frame = ctk.CTkFrame(self, **card_frame_kwargs())
        auto_frame.pack(fill="x", padx=14, pady=(0, 12))
        auto_controls = ctk.CTkFrame(auto_frame, fg_color="transparent")
        auto_controls.pack(fill="x", padx=14, pady=14)
        auto_controls.grid_columnconfigure(1, weight=1)
        auto_controls.grid_columnconfigure(5, weight=1)

        ctk.CTkLabel(
            auto_controls,
            text="安装对象",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self._remote_auto_provider_combo = ctk.CTkComboBox(
            auto_controls,
            values=list(self._remote_auto_options.keys()),
            width=160,
            command=lambda _value: self._on_remote_auto_provider_change(),
            **combo_style(),
        )
        self._remote_auto_provider_combo.grid(row=0, column=1, sticky="w", padx=(8, 12))
        self._remote_auto_provider_combo.set("Claude + Codex")

        action_bar = ctk.CTkFrame(auto_controls, fg_color="transparent")
        action_bar.grid(row=1, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        for column in range(3):
            action_bar.grid_columnconfigure(column, weight=0)

        check_button = ctk.CTkButton(
            action_bar,
            text="一致性检查",
            width=104,
            command=self._check_remote_auto_continue,
            **button_style("secondary"),
        )
        check_button.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        git_snapshot_button = ctk.CTkButton(
            action_bar,
            text="修复 Git 快照",
            width=116,
            command=self._install_remote_git_snapshot,
            **button_style("secondary"),
        )
        git_snapshot_button.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=(0, 4))
        install_button = ctk.CTkButton(
            action_bar,
            text="安装/修复全部",
            width=124,
            command=self._install_remote_auto_continue,
            **button_style("primary"),
        )
        install_button.grid(row=0, column=2, sticky="w", padx=(0, 8), pady=(0, 4))
        pause_button = ctk.CTkButton(
            action_bar,
            text="暂停 Stop 续跑",
            width=118,
            command=self._pause_remote_auto_continue,
            **button_style("warning"),
        )
        pause_button.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 4))
        uninstall_button = ctk.CTkButton(
            action_bar,
            text="卸载 Hook",
            width=88,
            command=self._uninstall_remote_auto_continue,
            **button_style("danger"),
        )
        uninstall_button.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=(0, 4))
        copy_diag_button = ctk.CTkButton(
            action_bar,
            text="复制诊断",
            width=96,
            command=self._copy_remote_auto_diagnostics,
            **button_style("secondary"),
        )
        copy_diag_button.grid(row=1, column=2, sticky="w", padx=(0, 8), pady=(0, 4))
        self._remote_auto_buttons = [
            check_button,
            git_snapshot_button,
            install_button,
            pause_button,
            uninstall_button,
            copy_diag_button,
        ]

        remote_switch_frame = ctk.CTkFrame(auto_controls, fg_color="transparent")
        remote_switch_frame.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(8, 0))
        for col in range(1, 5):
            remote_switch_frame.grid_columnconfigure(col, weight=0)
        ctk.CTkLabel(
            remote_switch_frame,
            text="远端开关",
            text_color=COLORS["muted"],
            font=font(12, "bold"),
        ).grid(row=0, column=0, rowspan=3, sticky="w", padx=(0, 10))

        def add_remote_switch(text, variable, feature, row, column, color="success"):
            switch = ctk.CTkSwitch(
                remote_switch_frame,
                text=text,
                variable=variable,
                command=lambda: self._toggle_remote_auto_feature(feature),
                text_color=COLORS["text"],
                progress_color=COLORS[color],
                button_color=COLORS["text"],
            )
            switch.grid(row=row, column=column, sticky="w", padx=(0, 14), pady=2)
            self._remote_auto_switches.append(switch)
            return switch

        add_remote_switch("Stop 续跑", self._remote_auto_continue_var, "auto_continue", 0, 1)
        add_remote_switch("训练达标续跑", self._remote_training_auto_continue_var, "training_auto_continue", 0, 2, color="accent")
        add_remote_switch("Git 快照开关", self._remote_git_snapshot_var, "git_snapshot", 0, 3)
        add_remote_switch("API 恢复", self._remote_error_recovery_var, "error_recovery", 0, 4)
        self._remote_git_snapshot_on_start_switch = add_remote_switch(
            "对话/消息/Stop 快照",
            self._remote_git_snapshot_on_start_var,
            "git_snapshot_on_start",
            1,
            1,
        )
        self._remote_git_snapshot_on_recovery_switch = add_remote_switch(
            "API 恢复快照",
            self._remote_git_snapshot_on_recovery_var,
            "git_snapshot_on_recovery",
            1,
            2,
        )
        self._remote_git_auto_push_switch = add_remote_switch(
            "快照后推送远端",
            self._remote_git_auto_push_var,
            "git_auto_push",
            1,
            3,
            color="accent",
        )
        self._remote_permission_auto_approve_switch = add_remote_switch(
            "权限自动确认",
            self._remote_permission_auto_approve_var,
            "permission_auto_approve",
            2,
            1,
            color="warning",
        )

        self._remote_auto_feature_label = ctk.CTkLabel(
            auto_controls,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._remote_auto_feature_label.grid(row=3, column=0, columnspan=8, sticky="ew", pady=(10, 0))
        bind_wraplength(auto_controls, self._remote_auto_feature_label, padding=20)

        self._remote_auto_status_label = ctk.CTkLabel(
            auto_controls,
            text="未检查。安装/修复会写入远端 hook 与设置；远端需具备 sh 和 Python 3.6+。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._remote_auto_status_label.grid(row=4, column=0, columnspan=8, sticky="ew", pady=(8, 0))
        bind_wraplength(auto_controls, self._remote_auto_status_label, padding=20)

        self.refresh()
        self._load_saved_proxy_subscription_ui()

    def destroy(self):
        self._cancel_proxy_startup_refresh()
        self._cancel_proxy_periodic_update()
        super().destroy()

    def refresh(self):
        if not self._cards_frame:
            return

        # Clear cards
        for w in self._cards_frame.winfo_children():
            w.destroy()

        profiles = profile_manager.list_ssh_profiles()
        active = profile_manager.get_active_ssh_name()

        if not profiles:
            EmptyState(
                self._cards_frame,
                "暂无 SSH 服务器",
                "添加一台服务器后，可以把本机配置同步到远程环境。",
                "新建服务器",
                self._create_server,
            ).pack(fill="x", pady=(12, 4))
        else:
            for p in profiles:
                is_active = p.name == active
                is_connected = ssh_manager.ssh_manager.is_connected(p.name)
                status = "已连接" if is_connected else "未连接"
                status_color = COLORS["success"] if is_connected else COLORS["muted_soft"]

                info = [
                    f"地址: {p.host}:{p.port}  |  用户: {p.username}  |  认证: {p.auth_type}",
                    f"状态: {status}",
                ]
                remote_dirs = []
                if getattr(p, "remote_claude_dir", None):
                    remote_dirs.append(f"Claude: {p.remote_claude_dir}")
                if getattr(p, "remote_codex_dir", None):
                    remote_dirs.append(f"Codex: {p.remote_codex_dir}")
                if remote_dirs:
                    info.append("远端目录: " + "  |  ".join(remote_dirs))

                card_frame = ctk.CTkFrame(
                    self._cards_frame,
                    **card_frame_kwargs(COLORS["success"] if is_connected else COLORS["border_soft"]),
                )
                card_frame.pack(fill="x", pady=5)

                # Header
                top = ctk.CTkFrame(card_frame, fg_color="transparent")
                top.pack(fill="x", padx=14, pady=(12, 4))

                selected_var = ctk.BooleanVar(value=p.name in self._selected_server_names)
                ctk.CTkCheckBox(
                    top,
                    text="批量操作",
                    width=84,
                    checkbox_width=18,
                    checkbox_height=18,
                    text_color=COLORS["muted"],
                    font=font(12),
                    variable=selected_var,
                    command=lambda n=p.name, v=selected_var: self._toggle_batch_server(n, v.get()),
                ).pack(side="left", padx=(0, 6))

                indicator = ctk.CTkLabel(top, text="●", text_color=status_color, font=font(15))
                indicator.pack(side="left")

                name_label = ctk.CTkLabel(top, text=p.name, text_color=COLORS["text"], font=font(15, "bold"))
                name_label.pack(side="left", padx=(7, 0))

                if is_active:
                    ctk.CTkLabel(
                        top,
                        text="当前",
                        fg_color=COLORS["primary"],
                        corner_radius=4,
                        text_color=COLORS["text"],
                        font=font(11, "bold"),
                        padx=7,
                        pady=1,
                    ).pack(side="left", padx=(8, 0))

                # Info
                info_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
                info_frame.pack(fill="x", padx=14, pady=(0, 8))
                for line in info:
                    lbl = ctk.CTkLabel(
                        info_frame,
                        text=line,
                        text_color=COLORS["muted"],
                        font=font(12),
                        anchor="w",
                        justify="left",
                    )
                    lbl.pack(fill="x")
                    bind_wraplength(info_frame, lbl, padding=4)

                # Buttons
                btn_frame = ctk.CTkFrame(card_frame, fg_color="transparent")
                btn_frame.pack(fill="x", padx=14, pady=(0, 12))

                if is_connected:
                    ctk.CTkButton(
                        btn_frame,
                        text="断开",
                        width=62,
                        command=lambda n=p.name: self._disconnect(n),
                        **button_style("danger", compact=True),
                    ).pack(side="left", padx=(0, 6))
                else:
                    ctk.CTkButton(
                        btn_frame,
                        text="连接",
                        width=62,
                        command=lambda n=p.name: self._connect(n),
                        **button_style("primary", compact=True),
                    ).pack(side="left", padx=(0, 6))

                ctk.CTkButton(
                    btn_frame,
                    text="编辑",
                    width=58,
                    command=lambda n=p.name: self._edit_server(n),
                    **button_style("secondary", compact=True),
                ).pack(side="left", padx=(0, 6))

                ctk.CTkButton(
                    btn_frame,
                    text="删除",
                    width=58,
                    command=lambda n=p.name: self._delete_server(n),
                    **button_style("danger", compact=True),
                ).pack(side="left")

        # Update server combo
        server_names = [p.name for p in profiles]
        self._selected_server_names.intersection_update(server_names)
        current_server = self._server_combo.get()
        self._server_combo.configure(values=server_names if server_names else ["(无)"])
        if server_names:
            selected_server = current_server if current_server in server_names else server_names[0]
            self._server_combo.set(selected_server)
            if selected_server != current_server:
                self._reset_remote_pull_options()
        else:
            self._server_combo.set("(无)")
            self._reset_remote_pull_options()
        self._update_batch_target_label(server_names)
        self._refresh_sync_profile_combo()
        self._update_remote_auto_feature_label()
        self._refresh_remote_auto_switch_availability()
        if not server_names:
            self._set_remote_auto_status("\u8bf7\u5148\u6dfb\u52a0\u5e76\u9009\u62e9 SSH \u670d\u52a1\u5668", severity="warning")

    def _create_server(self):
        def on_save(profile, _):
            ssh_manager.ssh_manager.disconnect(profile.name)
            profile_manager.save_ssh_profile(profile)
            show_toast(self.winfo_toplevel(), f"已创建: {profile.name}")
            self.refresh()

        SSHEditorDialog(self.winfo_toplevel(), title="新建 SSH 服务器", on_save=on_save)

    def _edit_server(self, name):
        profiles = profile_manager.list_ssh_profiles()
        profile = next((p for p in profiles if p.name == name), None)

        def on_save(new_profile, old_profile):
            previous_name = old_profile.name if old_profile else None
            if previous_name:
                ssh_manager.ssh_manager.disconnect(previous_name)
            ssh_manager.ssh_manager.disconnect(new_profile.name)
            profile_manager.save_ssh_profile(new_profile, previous_name=previous_name)
            show_toast(self.winfo_toplevel(), f"已保存: {new_profile.name}")
            self.refresh()

        SSHEditorDialog(self.winfo_toplevel(), title="编辑 SSH 服务器",
                        profile=profile, on_save=on_save)

    def _delete_server(self, name):
        def do_delete():
            ssh_manager.ssh_manager.disconnect(name)
            profile_manager.delete_ssh_profile(name)
            show_toast(self.winfo_toplevel(), f"已删除: {name}")
            self.refresh()

        ConfirmDialog(self.winfo_toplevel(), title="删除服务器",
                      message=f"确定要删除 \"{name}\" 吗？\n关联的密钥也会被清除。",
                      on_confirm=do_delete)

    def _set_sync_status(self, message: str, severity: str = "info"):
        if not self._sync_status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._sync_status_label.configure(text=message, text_color=color)

    def _profile_server_names(self) -> list[str]:
        return [p.name for p in profile_manager.list_ssh_profiles()]

    def _ordered_server_names(self, selected_names: set[str] | list[str] | tuple[str, ...]) -> list[str]:
        selected = set(selected_names)
        return [name for name in self._profile_server_names() if name in selected]

    def _format_server_target(self, server_names: list[str]) -> str:
        if len(server_names) == 1:
            return server_names[0]
        preview = "、".join(server_names[:3])
        suffix = "..." if len(server_names) > 3 else ""
        return f"{len(server_names)} 台服务器（{preview}{suffix}）"

    def _single_target_name_for_ui(self) -> str:
        if not self._server_combo:
            return ""
        server_name = str(self._server_combo.get() or "").strip()
        if not server_name or (server_name.startswith("(") and server_name.endswith(")")):
            return ""
        return server_name

    def _update_target_context_ui(self, selected: list[str] | None = None):
        selected = selected if selected is not None else self._ordered_server_names(self._selected_server_names)
        single_target = self._single_target_name_for_ui()
        if selected:
            summary = f"远端批量模式: {self._format_server_target(selected)}"
            hint = (
                "推送当前/所选、远端清理、Git 同步到 SSH、SSH AI 代理使用远端批量；"
                f"远端拉取、Git 检查/从 SSH 导入、远端自动续跑仍使用单台目标 {single_target or '未选择'}。"
            )
            summary_color = COLORS["accent"]
            current_text = "推送当前到批量"
            selected_text = "推送所选到批量"
        else:
            summary = f"单台模式: {single_target or '未选择服务器'}"
            hint = "推送/清理/Git 同步到 SSH/SSH AI 代理使用单台目标；要同时操作多台，请勾选服务器卡片里的“批量操作”。远端拉取始终只读取单台目标。"
            summary_color = COLORS["muted"] if single_target else COLORS["warning"]
            current_text = "推送当前到单台"
            selected_text = "推送所选到单台"

        if self._target_summary_label:
            self._target_summary_label.configure(text=summary, text_color=summary_color)
        if self._target_hint_label:
            self._target_hint_label.configure(text=hint)
        if self._sync_current_button:
            self._sync_current_button.configure(text=current_text)
        if self._sync_selected_button:
            self._sync_selected_button.configure(text=selected_text)
        self._update_proxy_target_label()

    def _update_batch_target_label(self, server_names: list[str] | None = None):
        all_names = server_names if server_names is not None else self._profile_server_names()
        self._selected_server_names.intersection_update(all_names)
        selected = [name for name in all_names if name in self._selected_server_names]
        if self._batch_target_label:
            if selected:
                self._batch_target_label.configure(
                    text=f"远端批量: {self._format_server_target(selected)}",
                    text_color=COLORS["accent"],
                )
            else:
                self._batch_target_label.configure(
                    text="远端批量: 未勾选（单台模式）",
                    text_color=COLORS["muted"],
                )
        action_state = "normal" if all_names else "disabled"
        for button in (self._batch_select_all_button, self._batch_clear_button):
            if button:
                try:
                    button.configure(state=action_state)
                except Exception:
                    pass
        self._update_target_context_ui(selected)

    def _toggle_batch_server(self, server_name: str, selected: bool):
        if selected:
            self._selected_server_names.add(server_name)
        else:
            self._selected_server_names.discard(server_name)
        self._update_batch_target_label()

    def _select_all_batch_servers(self):
        self._selected_server_names = set(self._profile_server_names())
        self._update_batch_target_label()
        self.refresh()

    def _clear_batch_servers(self):
        self._selected_server_names.clear()
        self._update_batch_target_label()
        self.refresh()

    def _selected_sync_server_names(self) -> list[str]:
        selected = self._ordered_server_names(self._selected_server_names)
        if selected:
            return selected
        server_name = self._selected_server_name()
        return [server_name] if server_name else []

    def _run_server_batch(self, server_names: list[str], action):
        results = []
        failures = []
        for server_name in server_names:
            try:
                result = action(server_name)
                results.append(_format_server_batch_item(server_name, result))
            except Exception as e:
                failures.append(f"{server_name}: {e}")
        return {"results": results, "failures": failures, "server_names": server_names}

    def _show_server_batch_result(self, payload, success_message: str):
        result = payload.get("result") or {}
        server_count = len(result.get("server_names", []) or [])
        operation_label = "批量操作" if server_count > 1 else "操作"
        if not payload["ok"]:
            message = f"{operation_label}失败: {payload['error']}"
            self._set_sync_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        results = result.get("results", [])
        failures = result.get("failures", [])
        if failures and results:
            message = " | ".join(results) + " | 部分失败: " + "；".join(failures)
            severity = "warning"
        elif failures:
            message = f"{operation_label}失败: " + "；".join(failures)
            severity = "error"
        else:
            message = " | ".join(results) if results else success_message
            severity = "success"
        self._set_sync_status(message, severity)
        show_toast(self.winfo_toplevel(), message, is_error=bool(failures))

    def _set_proxy_status(self, message: str, severity: str = "info"):
        if not self._proxy_status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._proxy_status_label.configure(text=message, text_color=color)

    def _set_proxy_cache_status(self, message: str, severity: str = "info"):
        if not self._proxy_cache_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._proxy_cache_label.configure(text=message, text_color=color)

    def _set_proxy_selected_summary(self, message: str, severity: str = "info"):
        if not self._proxy_selected_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._proxy_selected_label.configure(text=message, text_color=color)

    def _update_proxy_target_label(self):
        if not self._proxy_target_label:
            return
        selected = self._ordered_server_names(self._selected_server_names)
        if selected:
            self._proxy_target_label.configure(
                text=f"SSH 目标: {self._format_server_target(selected)}",
                text_color=COLORS["accent"],
            )
            return
        single_target = self._single_target_name_for_ui()
        if single_target:
            self._proxy_target_label.configure(text=f"SSH 目标: {single_target}", text_color=COLORS["muted"])
        else:
            self._proxy_target_label.configure(text="SSH 目标: 未选择服务器", text_color=COLORS["warning"])

    def _set_proxy_busy(self, busy: bool):
        self._proxy_busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self._proxy_fetch_button,
            self._proxy_latency_button,
            self._proxy_use_node_button,
            self._proxy_load_file_button,
            self._proxy_deploy_button,
            self._proxy_inspect_button,
            self._proxy_remote_test_button,
            self._proxy_remote_cleanup_button,
        ):
            if not button:
                continue
            try:
                if button in (self._proxy_use_node_button, self._proxy_latency_button) and not self._proxy_subscription_options:
                    button.configure(state="disabled")
                else:
                    button.configure(state=state)
            except Exception:
                pass
        if self._proxy_auto_refresh_check:
            try:
                self._proxy_auto_refresh_check.configure(state=state)
            except Exception:
                pass
        if self._proxy_periodic_update_check:
            try:
                self._proxy_periodic_update_check.configure(state=state)
            except Exception:
                pass
        if self._proxy_subscription_combo:
            try:
                combo_state = "disabled" if busy or not self._proxy_subscription_options else "normal"
                self._proxy_subscription_combo.configure(state=combo_state)
            except Exception:
                pass

    def _load_saved_proxy_subscription_ui(self):
        if self._proxy_saved_subscription_loaded:
            return
        self._proxy_saved_subscription_loaded = True
        state = remote_proxy.load_proxy_subscription_state()
        url = str(state.get("url") or "").strip()
        auto_refresh = remote_proxy.proxy_subscription_auto_refresh_enabled("ssh")
        periodic_update = bool(state.get("ssh_periodic_update_enabled"))
        interval_minutes = str(state.get("ssh_periodic_update_interval_minutes") or "60")
        self._proxy_auto_refresh_var.set(auto_refresh)
        self._proxy_periodic_update_var.set(periodic_update)

        if url and self._proxy_subscription_entry:
            self._proxy_subscription_entry.delete(0, "end")
            self._proxy_subscription_entry.insert(0, url)
        if self._proxy_periodic_update_entry:
            self._proxy_periodic_update_entry.delete(0, "end")
            self._proxy_periodic_update_entry.insert(0, interval_minutes)

        cached = remote_proxy.load_cached_proxy_subscription()
        if cached and cached.nodes:
            self._proxy_latency_results = {}
            self._proxy_latency_server_count = 0
            self._set_proxy_subscription_nodes(cached.nodes)
            self._select_proxy_subscription_node_by_key(str(state.get("selected_node_key") or ""))
            self._use_selected_proxy_subscription_node(show_message=False, persist_selection=False)
            self._set_proxy_cache_status(
                f"本机缓存: {len(cached.nodes)} 个节点；上次拉取 {state.get('last_fetched_at') or '-'}",
                "success",
            )
            self._set_proxy_status(
                f"已加载本机缓存订阅: {len(cached.nodes)} 个节点；上次拉取 {state.get('last_fetched_at') or '-'}"
            )
        elif url:
            self._set_proxy_cache_status("本机缓存: 未找到可用节点", "warning")
            self._set_proxy_status("已恢复订阅链接；尚未找到可用本机缓存，可手动拉取订阅。", "warning")

        if url and auto_refresh:
            self._schedule_proxy_startup_refresh()
        self._schedule_proxy_periodic_update(initial=True)

    def _select_proxy_subscription_node_by_key(self, node_key: str) -> bool:
        if not node_key or not self._proxy_subscription_combo:
            return False
        for label, item in self._proxy_subscription_options.items():
            if remote_proxy.proxy_node_key(item.node) == node_key:
                self._proxy_subscription_combo.set(label)
                return True
        return False

    def _on_proxy_auto_refresh_toggle(self):
        enabled = bool(self._proxy_auto_refresh_var.get())
        remote_proxy.set_proxy_subscription_auto_refresh(enabled, scope="ssh")
        if enabled:
            self._set_proxy_status("已开启 SSH 代理启动时刷新；下次打开 SSH 页会自动重新拉取订阅并保留可用缓存。", "success")
            if self._proxy_subscription_url_input():
                self._fetch_proxy_subscription(auto=True, show_message=False)
        else:
            self._cancel_proxy_startup_refresh()
            self._set_proxy_status("已关闭 SSH 代理启动时刷新。")

    def _schedule_proxy_startup_refresh(self):
        self._cancel_proxy_startup_refresh()
        self._proxy_startup_refresh_after_id = self.after(800, self._run_proxy_startup_refresh)

    def _run_proxy_startup_refresh(self):
        self._proxy_startup_refresh_after_id = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        if self._proxy_subscription_url_input() and bool(self._proxy_auto_refresh_var.get()):
            self._fetch_proxy_subscription(auto=True, show_message=False)

    def _cancel_proxy_startup_refresh(self):
        if not self._proxy_startup_refresh_after_id:
            return
        try:
            self.after_cancel(self._proxy_startup_refresh_after_id)
        except Exception:
            pass
        self._proxy_startup_refresh_after_id = None

    def _proxy_periodic_update_interval_minutes(self) -> int:
        raw = self._proxy_periodic_update_entry.get().strip() if self._proxy_periodic_update_entry else ""
        try:
            value = int(raw or "60")
        except ValueError:
            value = 60
        value = min(max(value, 5), 1440)
        if self._proxy_periodic_update_entry and raw != str(value):
            self._proxy_periodic_update_entry.delete(0, "end")
            self._proxy_periodic_update_entry.insert(0, str(value))
        return value

    def _on_proxy_periodic_update_toggle(self):
        enabled = bool(self._proxy_periodic_update_var.get())
        interval = self._proxy_periodic_update_interval_minutes()
        remote_proxy.save_proxy_subscription_state(
            ssh_periodic_update_enabled=enabled,
            ssh_periodic_update_interval_minutes=interval,
        )
        if enabled:
            self._set_proxy_status(f"已开启 SSH 代理定时热更新；每 {interval} 分钟拉取订阅，并无重启热更新正在运行的 SSH 代理。", "success")
        else:
            self._set_proxy_status("已关闭 SSH 代理定时热更新。")
        self._schedule_proxy_periodic_update(initial=not enabled)

    def _schedule_proxy_periodic_update(self, initial: bool = False):
        self._cancel_proxy_periodic_update()
        if not bool(self._proxy_periodic_update_var.get()):
            return
        interval_minutes = self._proxy_periodic_update_interval_minutes()
        delay_minutes = 1 if initial else interval_minutes
        remote_proxy.save_proxy_subscription_state(ssh_periodic_update_interval_minutes=interval_minutes)
        self._proxy_periodic_update_after_id = self.after(delay_minutes * 60 * 1000, self._run_proxy_periodic_update)

    def _cancel_proxy_periodic_update(self):
        if not self._proxy_periodic_update_after_id:
            return
        try:
            self.after_cancel(self._proxy_periodic_update_after_id)
        except Exception:
            pass
        self._proxy_periodic_update_after_id = None

    def _run_proxy_periodic_update(self):
        if not bool(self._proxy_periodic_update_var.get()):
            return
        if self._proxy_periodic_update_running or self._proxy_busy or self._ssh_busy:
            self._schedule_proxy_periodic_update()
            return
        url = self._proxy_subscription_url_input()
        if not url:
            self._set_proxy_status("SSH 代理定时更新跳过：尚未设置订阅链接。", "warning")
            self._schedule_proxy_periodic_update()
            return
        server_names = self._selected_sync_server_names()
        if not server_names:
            self._set_proxy_status("SSH 代理定时更新跳过：尚未选择 SSH 目标。", "warning")
            self._schedule_proxy_periodic_update()
            return
        self._proxy_periodic_update_running = True
        self._set_proxy_cache_status("本机缓存: SSH 定时更新中...")
        self._set_proxy_status(f"正在定时刷新订阅，并热更新 {self._format_server_target(server_names)} 上运行中的代理...")

        def run():
            try:
                result = remote_proxy.fetch_proxy_subscription(url)
                batch = self._run_server_batch(
                    server_names,
                    lambda server_name: remote_proxy.refresh_running_ai_proxy_from_subscription(server_name, result.nodes),
                )
                payload = {"ok": True, "result": result, "batch": batch, "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "batch": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._proxy_periodic_update_running = False
                if not payload["ok"]:
                    self._set_proxy_cache_status("本机缓存: SSH 定时更新失败，继续使用已有节点", "warning")
                    self._set_proxy_status(f"SSH 代理定时更新失败: {payload['error']}", "warning")
                    self._schedule_proxy_periodic_update()
                    return
                result = payload["result"]
                self._proxy_latency_results = {}
                self._proxy_latency_server_count = 0
                self._set_proxy_subscription_nodes(result.nodes)
                self._set_proxy_cache_status(f"本机缓存: SSH 定时更新已保存 {len(result.nodes)} 个节点", "success")
                batch = payload.get("batch") or {}
                results = batch.get("results", [])
                failures = batch.get("failures", [])
                if failures and results:
                    message = "SSH 代理定时更新部分成功: " + " | ".join(results) + " | 失败: " + "；".join(failures)
                    severity = "warning"
                elif failures:
                    message = "SSH 代理定时更新失败: " + "；".join(failures)
                    severity = "warning"
                else:
                    message = "SSH 代理定时更新完成: " + (" | ".join(results) if results else "无运行中代理需要更新")
                    severity = self._proxy_periodic_update_message_severity(message)
                self._set_proxy_status(message, severity)
                self._schedule_proxy_periodic_update()

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _proxy_periodic_update_message_severity(self, message: str) -> str:
        text = str(message or "")
        if any(marker in text for marker in ("失败", "未完全", "跳过", "不可用", "没有测到")):
            return "warning"
        return "success"

    def _proxy_subscription_url_input(self) -> str:
        if not self._proxy_subscription_entry:
            return ""
        return self._proxy_subscription_entry.get().strip()

    def _selected_proxy_subscription_node_key(self) -> str:
        if not self._proxy_subscription_combo:
            return ""
        selected = self._proxy_subscription_combo.get()
        item = self._proxy_subscription_options.get(selected)
        if not item:
            return ""
        return remote_proxy.proxy_node_key(item.node)

    def _set_proxy_subscription_nodes(self, nodes, preserve_key: str = ""):
        current_key = preserve_key or self._selected_proxy_subscription_node_key()
        self._proxy_subscription_nodes = list(
            remote_proxy.sort_proxy_subscription_nodes(nodes or [], self._proxy_latency_results)
        )
        options = {}
        for item in self._proxy_subscription_nodes:
            label = self._proxy_subscription_option_label(item)
            if label in options:
                label = f"{label} #{item.index}"
            options[label] = item
        self._proxy_subscription_options = options

        if not self._proxy_subscription_combo:
            return
        values = list(options.keys()) or ["没有识别到可用节点"]
        self._proxy_subscription_combo.configure(values=values, state="normal" if options else "disabled")
        self._proxy_subscription_combo.set(values[0])
        if current_key:
            self._select_proxy_subscription_node_by_key(current_key)
        if self._proxy_use_node_button:
            self._proxy_use_node_button.configure(state="normal" if options and not self._proxy_busy else "disabled")
        if self._proxy_latency_button:
            self._proxy_latency_button.configure(state="normal" if options and not self._proxy_busy else "disabled")

    def _proxy_subscription_option_label(self, item) -> str:
        key = remote_proxy.proxy_node_key(item.node)
        region = remote_proxy.proxy_node_region(item.node)
        latency = remote_proxy.proxy_node_latency_label(self._proxy_latency_results.get(key))
        if self._proxy_latency_server_count > 1:
            detail = remote_proxy.proxy_node_latency_detail(self._proxy_latency_results.get(key))
            if detail:
                latency = f"{detail} · {latency}"
        return f"【{region}】 {latency} · {item.display_name()}"

    def _fetch_proxy_subscription(self, auto: bool = False, show_message: bool = True):
        if self._proxy_busy:
            if show_message:
                show_toast(self.winfo_toplevel(), "订阅正在拉取中，请稍等", is_error=True)
            return
        url = self._proxy_subscription_url_input()
        if not url:
            message = "请先粘贴订阅链接"
            self._set_proxy_status(message, "warning")
            if show_message:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        self._set_proxy_busy(True)
        self._set_proxy_cache_status("本机缓存: 正在刷新订阅..." if auto else "本机缓存: 正在拉取订阅...")
        self._set_proxy_status("正在自动刷新订阅..." if auto else "正在拉取订阅并解析节点...")

        def run():
            try:
                payload = {"ok": True, "result": remote_proxy.fetch_proxy_subscription(url), "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._set_proxy_busy(False)
                if not payload["ok"]:
                    if auto and self._proxy_subscription_options:
                        message = f"自动刷新失败，已保留本机缓存: {payload['error']}"
                        severity = "warning"
                        self._set_proxy_cache_status("本机缓存: 自动刷新失败，继续使用已有节点", "warning")
                    else:
                        message = f"订阅拉取失败: {payload['error']}"
                        severity = "error"
                        self._set_proxy_cache_status("本机缓存: 拉取失败", "error")
                    self._set_proxy_status(message, severity)
                    if show_message:
                        show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                result = payload["result"]
                state = remote_proxy.load_proxy_subscription_state()
                self._proxy_latency_results = {}
                self._proxy_latency_server_count = 0
                self._set_proxy_subscription_nodes(result.nodes)
                if not self._select_proxy_subscription_node_by_key(str(state.get("selected_node_key") or "")):
                    self._use_selected_proxy_subscription_node(show_message=False)
                else:
                    self._use_selected_proxy_subscription_node(show_message=False, persist_selection=False)
                self._set_proxy_cache_status(
                    f"本机缓存: 已保存 {len(result.nodes)} 个节点；刚刚拉取",
                    "success",
                )
                message = f"订阅已保存到本机缓存；识别到 {len(result.nodes)} 个节点，已填入当前选择。"
                self._set_proxy_status(message, "success")
                if show_message:
                    show_toast(self.winfo_toplevel(), message)

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _measure_proxy_subscription_latencies(self):
        if self._proxy_busy:
            show_toast(self.winfo_toplevel(), "远端代理操作正在进行中，请稍等", is_error=True)
            return
        if not self._proxy_subscription_nodes:
            message = "请先拉取订阅，再对 SSH 目标测速"
            self._set_proxy_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        server_names = self._selected_sync_server_names()
        if not server_names:
            message = "请先选择单台服务器，或在上方服务器卡片勾选批量目标。"
            self._set_proxy_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        target_label = self._format_server_target(server_names)
        node_count = len(self._proxy_subscription_nodes)

        def done(payload):
            if not payload["ok"]:
                message = f"远端节点测速失败: {payload['error']}"
                self._set_proxy_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            result = payload.get("result") or {}
            failures = result.get("failures", [])
            server_results = result.get("results", {})
            self._proxy_latency_server_count = len(server_names)
            self._proxy_latency_results = self._aggregate_proxy_latency_results(server_results, len(server_names))
            self._set_proxy_subscription_nodes(self._proxy_subscription_nodes)
            fastest = self._fastest_proxy_subscription_node()
            ok_nodes = sum(1 for item in self._proxy_latency_results.values() if remote_proxy.proxy_node_latency_ok(item))
            if not fastest:
                message = f"{target_label}: 测速完成，但没有发现可连节点。"
                if failures:
                    message += " 失败: " + "；".join(failures)
                self._set_proxy_status(message, "warning")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            fastest_key = remote_proxy.proxy_node_key(fastest.node)
            self._select_proxy_subscription_node_by_key(fastest_key)
            self._use_selected_proxy_subscription_node(show_message=False)
            latency = remote_proxy.proxy_node_latency_label(self._proxy_latency_results.get(fastest_key))
            region = remote_proxy.proxy_node_region(fastest.node)
            detail = remote_proxy.proxy_node_latency_detail(self._proxy_latency_results.get(fastest_key))
            target_detail = f"{detail}，" if detail and len(server_names) > 1 else ""
            message = (
                f"{target_label}: 已完成 {node_count} 个节点远端测速，"
                f"{ok_nodes} 个可连；已选择最快节点【{region}】{target_detail}{latency}。"
            )
            if failures:
                message += " 部分服务器失败: " + "；".join(failures)
                severity = "warning"
            else:
                severity = "success"
            self._set_proxy_status(message, severity)
            show_toast(self.winfo_toplevel(), message, is_error=bool(failures))

        self._run_proxy_ssh_task(
            f"正在从 {target_label} 测试 {node_count} 个订阅节点延迟，完成后自动选择最低延迟节点...",
            lambda: self._measure_proxy_nodes_for_servers(server_names),
            on_done=done,
        )

    def _measure_proxy_nodes_for_servers(self, server_names: list[str]) -> dict:
        results = {}
        failures = []
        for server_name in server_names:
            try:
                results[server_name] = remote_proxy.measure_proxy_node_latencies_on_server(
                    server_name,
                    tuple(self._proxy_subscription_nodes),
                    timeout=3.0,
                    attempts=2,
                    max_workers=20,
                )
            except Exception as e:
                failures.append(f"{server_name}: {e}")
        return {"results": results, "failures": failures, "server_names": server_names}

    def _aggregate_proxy_latency_results(self, server_results: dict, server_count: int) -> dict:
        aggregate = {}
        for item in self._proxy_subscription_nodes:
            key = remote_proxy.proxy_node_key(item.node)
            latencies = []
            details = []
            attempts = 0
            for server_name, results in (server_results or {}).items():
                result = (results or {}).get(key)
                latency = remote_proxy.proxy_node_latency_ms(result)
                attempts = max(attempts, remote_proxy.proxy_node_latency_attempts(result))
                if latency is not None and remote_proxy.proxy_node_latency_ok(result):
                    latencies.append(latency)
                elif result is not None:
                    detail = remote_proxy.proxy_node_latency_detail(result)
                    if detail:
                        details.append(f"{server_name}: {detail}")
            if latencies:
                label = f"{len(latencies)}/{server_count} 可用" if server_count > 1 else ""
                aggregate[key] = remote_proxy.ProxyNodeLatencyResult(
                    node_key=key,
                    ok=True,
                    latency_ms=int(sum(latencies) / len(latencies)),
                    detail=label,
                    attempts=attempts,
                )
            else:
                aggregate[key] = remote_proxy.ProxyNodeLatencyResult(
                    node_key=key,
                    ok=False,
                    latency_ms=None,
                    detail="；".join(details[:2]),
                    attempts=attempts,
                )
        return aggregate

    def _fastest_proxy_subscription_node(self):
        fastest = None
        fastest_latency = None
        for item in self._proxy_subscription_nodes:
            result = self._proxy_latency_results.get(remote_proxy.proxy_node_key(item.node))
            latency = remote_proxy.proxy_node_latency_ms(result)
            if latency is None or not remote_proxy.proxy_node_latency_ok(result):
                continue
            if fastest is None or latency < fastest_latency:
                fastest = item
                fastest_latency = latency
        return fastest

    def _use_selected_proxy_subscription_node(self, show_message: bool = True, persist_selection: bool = True):
        if not self._proxy_subscription_combo:
            return
        selected = self._proxy_subscription_combo.get()
        item = self._proxy_subscription_options.get(selected)
        if not item:
            message = "请先拉取订阅并选择一个节点"
            self._set_proxy_status(message, "warning")
            if show_message:
                show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        node_text = remote_proxy.format_proxy_node(item.node)
        if self._proxy_node_text:
            self._proxy_node_text.delete("1.0", "end")
            self._proxy_node_text.insert("1.0", node_text)
        if persist_selection:
            remote_proxy.set_proxy_subscription_selected_node(item.node)
        node_summary = remote_proxy.describe_proxy_node(item.node)
        self._set_proxy_selected_summary(f"待部署节点: {node_summary}", "success")
        message = f"已填入待部署节点: {node_summary}"
        self._set_proxy_status(message, "success")
        if show_message:
            show_toast(self.winfo_toplevel(), message)

    def _proxy_node_input(self) -> str:
        if not self._proxy_node_text:
            return ""
        return self._proxy_node_text.get("1.0", "end").strip()

    def _load_proxy_node_file(self):
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
        if self._proxy_node_text:
            self._proxy_node_text.delete("1.0", "end")
            self._proxy_node_text.insert("1.0", content.strip())
        try:
            node_summary = remote_proxy.describe_proxy_node(remote_proxy.parse_proxy_node(content))
            self._set_proxy_selected_summary(f"待部署节点: {node_summary}", "success")
            self._set_proxy_status(f"已载入代理文件: {Path(path).name}；将使用节点 {node_summary}", "success")
        except Exception as e:
            self._set_proxy_selected_summary("待部署节点: 文件内容暂未识别", "warning")
            self._set_proxy_status(f"已载入代理文件: {Path(path).name}；暂未识别到可用节点: {e}", "warning")

    def _deploy_ai_proxy(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            message = "请先选择单台服务器，或在上方服务器卡片勾选批量目标。"
            self._set_proxy_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        proxy_text = self._proxy_node_input()
        try:
            proxy_node = remote_proxy.parse_proxy_node(proxy_text)
            node_summary = remote_proxy.describe_proxy_node(proxy_node)
            self._set_proxy_selected_summary(f"待部署节点: {node_summary}", "success")
        except Exception as e:
            message = f"代理节点格式不正确: {e}"
            self._set_proxy_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        target_label = self._format_server_target(server_names)

        def do_deploy():
            def done(payload):
                self._show_server_batch_result(payload, "AI 代理部署完成")
                if payload["ok"]:
                    result = payload.get("result") or {}
                    failures = result.get("failures", [])
                    severity = "warning" if failures and result.get("results") else "error" if failures else "success"
                    self._set_proxy_status(self._sync_status_label.cget("text"), severity)

            self._run_proxy_ssh_task(
                f"正在部署 AI 代理到 {target_label}，并验证 GPT/Claude/Gemini 连通性...",
                lambda: self._run_server_batch(
                    server_names,
                    lambda server_name: remote_proxy.install_ai_proxy_verified(
                        server_name,
                        proxy_text,
                        tuple(self._proxy_subscription_nodes),
                    ),
                ),
                on_done=done,
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="部署远端 AI 代理",
            message=(
                f"将把当前 Clash 节点写入 {target_label}，安装/复用 mihomo，"
                "并写入 VS Code Remote/Codex/Claude Code 远端环境入口。\n"
                f"识别到节点: {node_summary}\n"
                "规则只代理 OpenAI/ChatGPT、Claude/Anthropic、Gemini/Google AI 等域名，其余 DIRECT。\n"
                "部署后会立即做真实连通验证；如果当前节点不可用，会从订阅节点里按远端测速自动尝试可用节点。确定继续吗？"
            ),
            on_confirm=do_deploy,
        )

    def _inspect_ai_proxy(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            message = "请先选择单台服务器，或在上方服务器卡片勾选批量目标。"
            self._set_proxy_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        target_label = self._format_server_target(server_names)

        def done(payload):
            self._show_server_batch_result(payload, "AI 代理状态检查完成")
            if payload["ok"]:
                result = payload.get("result") or {}
                failures = result.get("failures", [])
                severity = "warning" if failures and result.get("results") else "error" if failures else "success"
                self._set_proxy_status(self._sync_status_label.cget("text"), severity)

        self._run_proxy_ssh_task(
            f"正在检查 {target_label} 的 AI 代理状态...",
            lambda: self._run_server_batch(
                server_names,
                lambda server_name: remote_proxy.inspect_ai_proxy(server_name).summary(),
            ),
            on_done=done,
        )

    def _probe_ai_proxy(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            message = "请先选择单台服务器，或在上方服务器卡片勾选批量目标。"
            self._set_proxy_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        target_label = self._format_server_target(server_names)

        def done(payload):
            self._show_server_batch_result(payload, "AI 代理连通性测试完成")
            if payload["ok"]:
                result = payload.get("result") or {}
                failures = result.get("failures", [])
                severity = "warning" if failures and result.get("results") else "error" if failures else "success"
                self._set_proxy_status(self._sync_status_label.cget("text"), severity)

        self._run_proxy_ssh_task(
            f"正在通过 {target_label} 的 AI 代理测试 OpenAI/Claude/Gemini 连通性...",
            lambda: self._run_server_batch(
                server_names,
                lambda server_name: remote_proxy.probe_ai_proxy(server_name),
            ),
            on_done=done,
        )

    def _cleanup_ai_proxy(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            message = "请先选择单台服务器，或在上方服务器卡片勾选批量目标。"
            self._set_proxy_status(message, "warning")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return
        target_label = self._format_server_target(server_names)

        def do_cleanup():
            def done(payload):
                self._show_server_batch_result(payload, "AI 代理清理完成")
                if payload["ok"]:
                    result = payload.get("result") or {}
                    failures = result.get("failures", [])
                    severity = "warning" if failures and result.get("results") else "error" if failures else "success"
                    self._set_proxy_status(self._sync_status_label.cget("text"), severity)

            self._run_proxy_ssh_task(
                f"正在清理 {target_label} 的 AI 代理配置...",
                lambda: self._run_server_batch(
                    server_names,
                    lambda server_name: remote_proxy.cleanup_ai_proxy(server_name),
                ),
                on_done=done,
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="清理远端 AI 代理",
            message=(
                f"将清理 {target_label} 上由本工具写入的远端 AI 代理入口，"
                "停止识别为 mihomo/clash 的代理进程，并移除 VS Code Remote 代理设置。\n"
                "检测到的旧 mihomo/clash 配置会先备份到 ~/.config/api-switcher/proxy-cleanup-backup-* 再移走。确定继续吗？"
            ),
            on_confirm=do_cleanup,
        )

    def _run_proxy_ssh_task(self, busy_message: str, worker, on_done=None):
        if self._proxy_busy:
            show_toast(self.winfo_toplevel(), "AI 代理操作正在进行中，请稍等", is_error=True)
            return
        if self._ssh_busy:
            show_toast(self.winfo_toplevel(), "SSH 操作正在进行中，请稍等", is_error=True)
            return
        self._set_proxy_busy(True)
        self._set_proxy_status(busy_message)

        def finish(payload):
            self._set_proxy_busy(False)
            if on_done:
                on_done(payload)

        self._run_ssh_task(busy_message, worker, on_done=finish)

    def _reset_remote_pull_options(self, message: str | None = None):
        self._remote_config_candidates = []
        self._remote_pull_options = {}
        self._remote_pull_server_name = None
        if self._remote_pull_combo:
            value = "请先读取远端配置"
            self._remote_pull_combo.configure(values=[value], state="disabled")
            self._remote_pull_combo.set(value)
        if self._remote_pull_type_combo:
            self._remote_pull_type_combo.configure(state="disabled")
            self._remote_pull_type_combo.set("全部项目")
        if self._remote_pull_button:
            self._remote_pull_button.configure(state="disabled")
        if self._remote_pull_hint:
            self._remote_pull_hint.configure(
                text=message
                or "远端拉取只读取单台目标，不受批量勾选影响；先读取实际存在的配置，再按 API/账号或 Claude/Codex 过滤。",
                text_color=COLORS["muted"],
            )

    def _on_server_selection_change(self):
        self._reset_remote_pull_options()
        self._update_target_context_ui()
        self._on_remote_auto_provider_change()

    def _set_remote_pull_candidates(self, candidates, server_name: str):
        self._remote_config_candidates = list(candidates or [])
        self._remote_pull_server_name = server_name
        if self._remote_pull_type_combo:
            self._remote_pull_type_combo.configure(state="normal")
            self._remote_pull_type_combo.set("全部项目")
        self._refresh_remote_pull_combo()

    def _remote_pull_filter(self) -> str:
        if not self._remote_pull_type_combo:
            return "all"
        return self._remote_pull_type_options.get(self._remote_pull_type_combo.get(), "all")

    def _remote_pull_filtered_candidates(self):
        selected_filter = self._remote_pull_filter()
        candidates = [candidate for candidate in self._remote_config_candidates if candidate.importable]
        if selected_filter == "api":
            return [candidate for candidate in candidates if candidate.category == "api"]
        if selected_filter == "account":
            return [candidate for candidate in candidates if candidate.category == "account"]
        if selected_filter in {"claude", "codex"}:
            return [candidate for candidate in candidates if candidate.product == selected_filter]
        return candidates

    def _refresh_remote_pull_combo(self):
        filtered = self._remote_pull_filtered_candidates()
        options = {}
        if len(filtered) > 1:
            label = self._remote_pull_all_label
            selected_filter = self._remote_pull_filter()
            if selected_filter == "api":
                label = "全部可拉取 API"
            elif selected_filter == "account":
                label = "全部可拉取账号"
            elif selected_filter == "claude":
                label = "全部 Claude 可拉取项"
            elif selected_filter == "codex":
                label = "全部 Codex 可拉取项"
            options[label] = tuple(candidate.kind for candidate in filtered)
        for candidate in filtered:
            options[candidate.display_name()] = (candidate.kind,)

        if self._remote_pull_combo:
            values = list(options.keys()) or ["没有可拉取配置"]
            self._remote_pull_combo.configure(values=values, state="normal" if options else "disabled")
            self._remote_pull_combo.set(values[0])
        if self._remote_pull_button:
            self._remote_pull_button.configure(state="normal" if options else "disabled")
        self._remote_pull_options = options
        self._update_remote_pull_hint(filtered)

    def _update_remote_pull_hint(self, filtered=None):
        if not self._remote_pull_hint:
            return
        candidates = self._remote_config_candidates
        if not candidates:
            self._remote_pull_hint.configure(
                text="远端拉取只读取单台目标，不受批量勾选影响；先读取实际存在的配置，再按 API/账号或 Claude/Codex 过滤。",
                text_color=COLORS["muted"],
            )
            return

        importable = [candidate for candidate in candidates if candidate.importable]
        filtered = self._remote_pull_filtered_candidates() if filtered is None else list(filtered)
        api_count = len([candidate for candidate in importable if candidate.category == "api"])
        account_count = len([candidate for candidate in importable if candidate.category == "account"])
        selected_label = self._remote_pull_type_combo.get() if self._remote_pull_type_combo else "全部项目"
        server_text = self._remote_pull_server_name or "-"

        lines = [
            f"已读取 {server_text}: 可拉取 {len(importable)}/{len(candidates)} 项（API {api_count}，账号 {account_count}）。当前范围「{selected_label}」可拉取 {len(filtered)} 项。"
        ]
        detail_lines = []
        for candidate in candidates:
            marker = "可拉取" if candidate.importable else "跳过"
            detail = candidate.reason
            if candidate.provider_label or candidate.model:
                pieces = [piece for piece in [candidate.provider_label, candidate.model] if piece]
                detail = " / ".join(pieces) + (f"；{candidate.reason}" if candidate.reason else "")
            detail_lines.append(f"{candidate.label} [{marker}]: {detail}")
        lines.extend(detail_lines[:4])
        self._remote_pull_hint.configure(
            text="\n".join(lines),
            text_color=COLORS["muted"] if importable else COLORS["warning"],
        )

    def _run_ssh_task(self, busy_message: str, worker, on_done=None, refresh: bool = False):
        if self._ssh_busy:
            show_toast(self.winfo_toplevel(), "SSH 操作正在进行中，请稍等", is_error=True)
            return

        self._ssh_busy = True
        self._set_sync_status(busy_message)
        if self._remote_inspect_button:
            self._remote_inspect_button.configure(state="disabled")
        if self._remote_pull_button:
            self._remote_pull_button.configure(state="disabled")

        def run():
            try:
                payload = {"ok": True, "result": worker(), "error": None}
            except Exception as e:
                payload = {"ok": False, "result": None, "error": str(e)}

            def finish():
                if not self.winfo_exists():
                    return
                self._ssh_busy = False
                if self._remote_inspect_button:
                    self._remote_inspect_button.configure(state="normal")
                if on_done:
                    on_done(payload)
                elif payload["ok"]:
                    message = str(payload["result"] or "操作完成")
                    self._set_sync_status(message, "success")
                    show_toast(self.winfo_toplevel(), message)
                else:
                    message = f"操作失败: {payload['error']}"
                    self._set_sync_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                if self._remote_pull_button and self._remote_pull_options:
                    self._remote_pull_button.configure(state="normal")
                if refresh:
                    self.refresh()

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _connect(self, name):
        profiles = profile_manager.list_ssh_profiles()
        profile = next((p for p in profiles if p.name == name), None)
        if not profile:
            show_toast(self.winfo_toplevel(), f"未找到服务器: {name}", is_error=True)
            return

        self._run_ssh_task(
            f"正在连接 {profile.host}:{profile.port}...",
            lambda: (ssh_manager.ssh_manager.connect(profile), f"已连接到 {profile.host}")[1],
            refresh=True,
        )

    def _disconnect(self, name):
        ssh_manager.ssh_manager.disconnect(name)
        show_toast(self.winfo_toplevel(), f"已断开连接: {name}")
        self.refresh()

    def _sync_current(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            return

        wire_api_mode = self._selected_codex_wire_api_mode()
        target_label = self._format_server_target(server_names)
        self._run_ssh_task(
            f"正在推送当前生效配置到 {target_label}...",
            lambda: self._run_server_batch(
                server_names,
                lambda server_name: sync_manager.sync_all_to_server(
                    server_name,
                    codex_wire_api_mode=wire_api_mode,
                ),
            ),
            on_done=lambda payload: self._show_server_batch_result(payload, "当前生效配置推送完成"),
        )

    def _selected_sync_kind(self) -> str:
        if not self._sync_kind_combo:
            return "claude_api"
        return self._sync_kind_options.get(self._sync_kind_combo.get(), "claude_api")

    def _on_sync_kind_change(self):
        self._refresh_sync_profile_combo()
        self._update_codex_wire_hint()

    def _selected_codex_wire_api_mode(self) -> str:
        if not self._codex_wire_api_combo:
            return "auto"
        return self._codex_wire_api_options.get(self._codex_wire_api_combo.get(), "auto")

    def _selected_clear_api_target(self) -> str:
        if not self._clear_api_combo:
            return "all"
        return self._clear_api_options.get(self._clear_api_combo.get(), "all")

    def _update_remote_auto_feature_label(self):
        if not self._remote_auto_feature_label:
            return
        parts = []
        for provider in self._selected_remote_auto_targets():
            settings = auto_continue_manager.get_settings(provider)
            label = "Claude" if provider == "claude" else "Codex"
            if not settings:
                parts.append(f"{label}: 本机未保存设置")
                continue
            feature_parts = [
                f"Stop续跑 {'ON' if settings.enabled else 'OFF'}",
                f"训练续跑 {'ON' if settings.training_auto_continue_enabled else 'OFF'}",
                f"训练模板 {training_prompt_template_by_key(settings.training_prompt_template_key)['name']}",
                f"Git快照开关 {'ON' if settings.git_auto_snapshot else 'OFF'}",
                f"API错误恢复 {'ON' if settings.error_recovery_enabled else 'OFF'}",
            ]
            feature_parts.append(f"快照后推送 {'ON' if settings.git_auto_push else 'OFF'}")
            if provider == "claude":
                feature_parts.append(f"权限自动确认 {'ON' if settings.auto_approve_permission_requests else 'OFF'}")
                feature_parts.append(f"Subagent {'ON' if settings.apply_to_subagents else 'OFF'}")
            parts.append(f"{label}: " + " / ".join(feature_parts))
        self._remote_auto_feature_label.configure(
            text=(
                "安装/修复会把本机设置和训练 Prompt 模板同步到远端；"
                "上方开关会立即写入已选 SSH 服务器。暂停只关闭 Stop 续跑，不影响 Git 快照、API 恢复或权限自动确认。"
                + (" | " if parts else "")
                + " | ".join(parts)
            )
        )

    def _update_codex_wire_hint(self):
        if not self._codex_wire_api_hint:
            return
        if self._selected_sync_kind() == "codex_api":
            text = "影响“推送所选”和“推送当前生效”里的 Codex API；远端自测会在服务器上各跑 3 次并回写最稳选项。"
        else:
            text = "仅影响“推送当前生效”里的 Codex API；推送 Claude 或账号快照时会自动忽略。"
        self._codex_wire_api_hint.configure(text=text)

    def _profile_names_for_kind(self, kind: str) -> list[str]:
        if kind == "claude_api":
            return [p.name for p in profile_manager.list_switchable_claude_profiles()]
        if kind == "claude_account":
            return [p.name for p in profile_manager.list_claude_account_profiles()]
        if kind == "codex_api":
            return [p.name for p in profile_manager.list_switchable_codex_profiles()]
        if kind == "codex_account":
            return [p.name for p in profile_manager.list_codex_account_profiles()]
        return []

    def _refresh_sync_profile_combo(self):
        if not self._profile_combo:
            return
        current_profile = self._profile_combo.get()
        profile_names = self._profile_names_for_kind(self._selected_sync_kind())
        self._profile_combo.configure(values=profile_names if profile_names else ["(无)"])
        if profile_names:
            self._profile_combo.set(current_profile if current_profile in profile_names else profile_names[0])
        else:
            self._profile_combo.set("(无)")
        self._update_codex_wire_hint()

    def _sync_selected(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            return

        profile_name = self._profile_combo.get()
        if profile_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择要推送的 API 或账号", is_error=True)
            return

        kind = self._selected_sync_kind()
        wire_api_mode = self._selected_codex_wire_api_mode()
        target_label = self._format_server_target(server_names)

        def do_sync():
            self._run_ssh_task(
                f"正在推送 {profile_name} 到 {target_label}...",
                lambda: self._run_server_batch(
                    server_names,
                    lambda server_name: sync_manager.sync_selected_to_server(
                        server_name,
                        kind,
                        profile_name,
                        codex_wire_api_mode=wire_api_mode,
                    ),
                ),
                on_done=lambda payload: self._show_server_batch_result(payload, f"{profile_name} 推送完成"),
            )

        if kind in {"claude_account", "codex_account"}:
            ConfirmDialog(
                self.winfo_toplevel(),
                title="确认推送账号",
                message=f"将把 \"{profile_name}\" 的官方登录凭据写入 {target_label}。\n确定继续吗？",
                on_confirm=do_sync,
            )
            return

        do_sync()

    def _clear_remote_api_info(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            return

        target = self._selected_clear_api_target()
        target_label = self._clear_api_combo.get() if self._clear_api_combo else "Claude + Codex"
        server_label = self._format_server_target(server_names)

        def do_clear():
            self._run_ssh_task(
                f"正在清除 {server_label} 上的 {target_label} 信息...",
                lambda: self._run_server_batch(
                    server_names,
                    lambda server_name: sync_manager.clear_remote_api_info(server_name, target),
                ),
                on_done=lambda payload: self._show_server_batch_result(payload, f"{target_label} 信息已清除"),
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="清除远端 API 信息",
            message=(
                f"将清除 {server_label} 上当前 {target_label} 的 API Key/Token、"
                "Base URL 覆盖和相关远端环境变量。\n"
                "此操作不会删除本机保存的 Profile。确定继续吗？"
            ),
            on_confirm=do_clear,
        )

    def _set_git_login_status(self, message: str, severity: str = "info"):
        if not self._git_login_status_label:
            return
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self._git_login_status_label.configure(text=message, text_color=color)

    def _inspect_git_login(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        def done(payload):
            if not payload["ok"]:
                message = f"Git 登录检查失败: {payload['error']}"
                self._set_git_login_status(message, "error")
                self._set_sync_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            status = payload["result"]
            message = status.summary()
            severity = "success" if status.remote_git_available else "warning"
            self._set_git_login_status(message, severity)
            self._set_sync_status("Git 登录状态已检查", severity)
            show_toast(self.winfo_toplevel(), "Git 登录状态已检查")

        self._run_ssh_task(
            f"正在检查 {server_name} 的 Git 登录状态...",
            lambda: remote_git_login.inspect_git_login(server_name),
            on_done=done,
        )

    def _sync_git_login(self):
        server_names = self._selected_sync_server_names()
        if not server_names:
            return
        target_label = self._format_server_target(server_names)

        def do_sync():
            def done(payload):
                if not payload["ok"]:
                    message = f"Git 登录同步失败: {payload['error']}"
                    self._set_git_login_status(message, "error")
                    self._set_sync_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                result = payload.get("result") or {}
                failures = result.get("failures", [])
                results = result.get("results", [])
                if failures and results:
                    message = " | ".join(results) + " | 部分失败: " + "；".join(failures)
                    severity = "warning"
                elif failures:
                    message = "Git 登录同步失败: " + "；".join(failures)
                    severity = "error"
                else:
                    message = " | ".join(results) if results else "Git 登录同步完成"
                    severity = "success"
                self._set_git_login_status(message, severity)
                self._set_sync_status(message, severity)
                show_toast(self.winfo_toplevel(), message, is_error=bool(failures))

            self._run_ssh_task(
                f"正在同步本机 Git 登录到 {target_label}...",
                lambda: self._run_server_batch(
                    server_names,
                    lambda server_name: remote_git_login.sync_git_login_to_server(server_name),
                ),
                on_done=done,
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="同步 Git 登录到 SSH",
            message=(
                f"将把本机 Git 用户名/邮箱写入 {target_label} 的全局 Git 配置。\n"
                "如果本机或远端没有 GitHub CLI，会尝试自动安装 gh；本机安装后仍需要已完成 gh auth login，"
                "才能把 token 通过 SSH 标准输入发送到远端执行 gh auth login。\n\n"
                "不会读取或复制 Windows Git Credential Manager 内部凭据。确定继续吗？"
            ),
            on_confirm=do_sync,
        )

    def _import_git_login(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        def do_import():
            def done(payload):
                if not payload["ok"]:
                    message = f"Git 登录导入失败: {payload['error']}"
                    self._set_git_login_status(message, "error")
                    self._set_sync_status(message, "error")
                    show_toast(self.winfo_toplevel(), message, is_error=True)
                    return

                message = payload["result"]
                self._set_git_login_status(message, "success")
                self._set_sync_status(message, "success")
                show_toast(self.winfo_toplevel(), message)

            self._run_ssh_task(
                f"正在从 {server_name} 导入 Git 登录到本机...",
                lambda: remote_git_login.sync_git_login_from_server(server_name),
                on_done=done,
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="从 SSH 导入 Git 登录",
            message=(
                f"将读取服务器 \"{server_name}\" 的 Git 用户名/邮箱并写入本机全局 Git 配置。\n"
                "如果远端 gh 已登录，还会读取远端 gh auth token，并在本机执行 gh auth login。"
                "本机没有 gh 时会尝试自动安装；不会读取 Windows Git Credential Manager 内部凭据。\n\n"
                "确定继续吗？"
            ),
            on_confirm=do_import,
        )

    def _selected_remote_auto_targets(self) -> list[str]:
        if not self._remote_auto_provider_combo:
            return ["claude", "codex"]
        selected = self._remote_auto_options.get(self._remote_auto_provider_combo.get(), "all")
        if selected == "all":
            return ["claude", "codex"]
        return [selected]

    def _has_selected_server(self) -> bool:
        if not self._server_combo:
            return False
        server_name = str(self._server_combo.get() or "").strip()
        return bool(server_name) and not (server_name.startswith("(") and server_name.endswith(")"))

    def _on_remote_auto_provider_change(self):
        self._update_remote_auto_feature_label()
        cached = self._cached_remote_auto_statuses_for_selection()
        if cached:
            self._refresh_remote_auto_switches_from_statuses(cached)
        else:
            self._refresh_remote_auto_switch_availability()

    def _cached_remote_auto_statuses_for_selection(self):
        if not self._server_combo:
            return []
        server_name = self._server_combo.get()
        targets = self._selected_remote_auto_targets()
        statuses = []
        for provider in targets:
            status = self._remote_auto_last_statuses.get((server_name, provider))
            if not status:
                return []
            statuses.append(status)
        return statuses

    def _refresh_remote_auto_switch_availability(self):
        action_state = "normal" if self._has_selected_server() and not self._remote_auto_busy else "disabled"
        for button in self._remote_auto_buttons:
            try:
                button.configure(state=action_state)
            except Exception:
                pass
        for switch in self._remote_auto_switches:
            try:
                switch.configure(state=action_state)
            except Exception:
                pass
        targets = self._selected_remote_auto_targets()
        permission_state = action_state if "claude" in targets else "disabled"
        if self._remote_permission_auto_approve_switch:
            try:
                self._remote_permission_auto_approve_switch.configure(state=permission_state)
            except Exception:
                pass
        if "claude" not in targets and not self._remote_auto_refreshing:
            self._remote_permission_auto_approve_var.set(False)
        git_trigger_state = action_state if bool(self._remote_git_snapshot_var.get()) else "disabled"
        for switch in (
            self._remote_git_snapshot_on_start_switch,
            self._remote_git_snapshot_on_recovery_switch,
            self._remote_git_auto_push_switch,
        ):
            if switch:
                try:
                    switch.configure(state=git_trigger_state)
                except Exception:
                    pass

    def _remote_auto_statuses_cover_selection(self, statuses) -> bool:
        if not statuses:
            return False
        selected_targets = set(self._selected_remote_auto_targets())
        status_targets = {status.provider_name for status in statuses}
        return selected_targets.issubset(status_targets)

    def _remote_auto_var_for_feature(self, feature: str):
        return {
            "auto_continue": self._remote_auto_continue_var,
            "training_auto_continue": self._remote_training_auto_continue_var,
            "git_snapshot": self._remote_git_snapshot_var,
            "git_snapshot_on_start": self._remote_git_snapshot_on_start_var,
            "git_snapshot_on_recovery": self._remote_git_snapshot_on_recovery_var,
            "git_auto_push": self._remote_git_auto_push_var,
            "error_recovery": self._remote_error_recovery_var,
            "permission_auto_approve": self._remote_permission_auto_approve_var,
        }.get(feature)

    def _set_remote_auto_feature_var(self, feature: str, value: bool):
        var = self._remote_auto_var_for_feature(feature)
        if not var:
            return
        self._remote_auto_refreshing = True
        try:
            var.set(bool(value))
        finally:
            self._remote_auto_refreshing = False

    def _refresh_remote_auto_switches_from_statuses(self, statuses):
        if not statuses:
            self._refresh_remote_auto_switch_availability()
            return

        if not self._remote_auto_statuses_cover_selection(statuses):
            self._refresh_remote_auto_switch_availability()
            return

        claude_statuses = [status for status in statuses if status.provider_name == "claude"]

        def all_enabled(attr: str) -> bool:
            return bool(statuses) and all(bool(getattr(status, attr, False)) for status in statuses)

        self._remote_auto_refreshing = True
        try:
            self._remote_auto_continue_var.set(all_enabled("enabled"))
            self._remote_training_auto_continue_var.set(all_enabled("training_auto_continue_enabled"))
            self._remote_git_snapshot_var.set(all_enabled("git_snapshot_master_enabled"))
            self._remote_git_snapshot_on_start_var.set(all_enabled("git_snapshot_on_start_enabled"))
            self._remote_git_snapshot_on_recovery_var.set(all_enabled("git_snapshot_on_recovery_enabled"))
            self._remote_git_auto_push_var.set(all_enabled("git_auto_push_enabled"))
            self._remote_error_recovery_var.set(all_enabled("error_recovery_enabled"))
            self._remote_permission_auto_approve_var.set(
                bool(claude_statuses)
                and all(bool(status.permission_auto_approve_enabled) for status in claude_statuses)
            )
        finally:
            self._remote_auto_refreshing = False
        self._refresh_remote_auto_switch_availability()

    def _selected_server_name(self) -> str | None:
        server_name = self._server_combo.get()
        if not self._has_selected_server():
            show_toast(self.winfo_toplevel(), "\u8bf7\u5148\u9009\u62e9\u670d\u52a1\u5668", is_error=True)
            return None
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return None
        return server_name

    def _set_remote_auto_status(self, message: str, is_error: bool = False, severity: str | None = None):
        if self._remote_auto_status_label:
            level = severity or ("error" if is_error else "info")
            color = {
                "error": COLORS["danger"],
                "warning": COLORS["warning"],
            }.get(level, COLORS["muted"])
            self._remote_auto_status_label.configure(
                text=message,
                text_color=color,
            )

    def _set_remote_auto_busy(self, busy: bool, message: str | None = None):
        self._remote_auto_busy = busy
        if self._remote_auto_provider_combo:
            try:
                state = "disabled" if busy else "normal"
                self._remote_auto_provider_combo.configure(state=state)
            except Exception:
                pass
        self._refresh_remote_auto_switch_availability()
        if message:
            self._set_remote_auto_status(message)

    def _run_remote_auto_task(self, busy_message: str, worker, on_done):
        if self._remote_auto_busy:
            show_toast(self.winfo_toplevel(), "远端自动续跑操作正在进行中，请稍等", is_error=True)
            return

        self._set_remote_auto_busy(True, busy_message)

        def run():
            try:
                payload = worker()
            except Exception as e:
                payload = {
                    "results": [],
                    "statuses": [],
                    "failures": [str(e)],
                }

            def finish():
                if not self.winfo_exists():
                    return
                self._set_remote_auto_busy(False)
                on_done(payload)

            try:
                self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _summarize_remote_auto_status(self, statuses, failures: list[str] | None = None) -> str:
        parts = [status.summary() for status in statuses]
        if failures:
            parts.append("失败: " + "；".join(failures))
        return " | ".join(parts) if parts else "没有可显示的远端自动续跑状态"

    def _format_remote_auto_diagnostics(self, statuses=None, failures: list[str] | None = None) -> str:
        if statuses is None:
            statuses = self._cached_remote_auto_statuses_for_selection()
        failures = failures or []
        server_name = self._server_combo.get() if self._server_combo else ""
        targets = ", ".join(self._selected_remote_auto_targets())
        lines = [
            f"SSH: {server_name or '-'}",
            f"Targets: {targets or '-'}",
            "",
        ]
        if failures:
            lines.append("Failures:")
            lines.extend(f"- {failure}" for failure in failures)
            lines.append("")

        if not statuses:
            lines.append("No cached consistency status. Run 一致性检查 first.")
            return "\n".join(lines)

        for status in statuses:
            issues = getattr(status, "issues", []) or []
            lines.extend([
                f"[{getattr(status, 'label', status.provider_name)}]",
                f"ready={status.ready}",
                f"config_dir={status.config_dir}",
                f"script_path={status.script_path}",
                f"settings_path={status.settings_path}",
                f"enabled={status.enabled}",
                f"training_auto_continue={status.training_auto_continue_enabled}",
                f"git_snapshot={status.git_snapshot_enabled}",
                f"git_auto_push={getattr(status, 'git_auto_push_enabled', False)}",
                f"error_recovery={status.error_recovery_enabled}",
                f"permission_auto_approve={status.permission_auto_approve_enabled}",
                f"runtime_ready={status.runtime_ready}",
                f"git_available={status.git_available}",
                f"hook_script_exists={status.hook_script_exists}",
                f"hook_registered={status.hook_registered}",
                f"settings_valid={status.settings_valid}",
                f"hook_script_mode={oct(status.hook_script_mode) if status.hook_script_mode is not None else '-'}",
                f"hook_script_sha256={status.hook_script_sha256 or '-'}",
                f"expected_hook_script_sha256={status.expected_hook_script_sha256 or '-'}",
                f"hook_script_matches_expected={status.hook_script_matches_expected}",
                f"settings_sha256={getattr(status, 'settings_sha256', '') or '-'}",
                f"expected_settings_sha256={getattr(status, 'expected_settings_sha256', '') or '-'}",
                f"settings_matches_expected={getattr(status, 'settings_matches_expected', None)}",
                f"codex_hooks_enabled={status.codex_hooks_enabled}",
                f"permission_mode={status.permission_mode or '-'}",
                "issues=" + ("; ".join(issues) if issues else "-"),
                "",
            ])
        return "\n".join(lines).strip()

    def _copy_remote_auto_diagnostics(self):
        payload = self._remote_auto_last_payload or {}
        statuses = payload.get("statuses") or self._cached_remote_auto_statuses_for_selection()
        failures = payload.get("failures") or []
        text = self._format_remote_auto_diagnostics(statuses, failures)
        self.clipboard_clear()
        self.clipboard_append(text)
        if statuses or failures:
            show_toast(self.winfo_toplevel(), "远端自动续跑诊断已复制")
        else:
            show_toast(self.winfo_toplevel(), "还没有诊断结果，请先点一致性检查", is_error=True)

    def _collect_remote_auto_statuses(self, server_name: str, targets: list[str]) -> tuple[list, list[str]]:
        statuses = []
        failures = []
        for provider in targets:
            try:
                statuses.append(remote_auto_continue.get_remote_auto_continue_status(server_name, provider))
            except Exception as e:
                failures.append(f"{provider}: {e}")
        return statuses, failures

    def _show_remote_auto_result(self, payload, default_message: str, expect_ready: bool = False):
        statuses = payload.get("statuses", [])
        failures = payload.get("failures", [])
        results = payload.get("results", [])
        self._remote_auto_last_payload = payload
        message = self._summarize_remote_auto_status(statuses, failures)
        has_not_ready = expect_ready and any(not status.ready for status in statuses)
        severity = "error" if failures else "warning" if has_not_ready else "info"
        self._set_remote_auto_status(message, severity=severity)
        if self._server_combo:
            server_name = self._server_combo.get()
            for status in statuses:
                self._remote_auto_last_statuses[(server_name, status.provider_name)] = status
        self._refresh_remote_auto_switches_from_statuses(statuses)
        toast_message = " | ".join(results)
        if failures:
            toast_message = (toast_message + " | " if toast_message else "") + "失败: " + "；".join(failures)
        show_toast(self.winfo_toplevel(), toast_message or default_message, is_error=bool(failures))

    def _toggle_remote_auto_feature(self, feature: str):
        if self._remote_auto_refreshing:
            return

        server_name = self._selected_server_name()
        if not server_name:
            self._refresh_remote_auto_switch_availability()
            return

        targets = self._selected_remote_auto_targets()
        value_by_feature = {
            "auto_continue": bool(self._remote_auto_continue_var.get()),
            "training_auto_continue": bool(self._remote_training_auto_continue_var.get()),
            "git_snapshot": bool(self._remote_git_snapshot_var.get()),
            "git_snapshot_on_start": bool(self._remote_git_snapshot_on_start_var.get()),
            "git_snapshot_on_recovery": bool(self._remote_git_snapshot_on_recovery_var.get()),
            "git_auto_push": bool(self._remote_git_auto_push_var.get()),
            "error_recovery": bool(self._remote_error_recovery_var.get()),
            "permission_auto_approve": bool(self._remote_permission_auto_approve_var.get()),
        }
        field_by_feature = {
            "auto_continue": "enabled",
            "training_auto_continue": "training_auto_continue_enabled",
            "git_snapshot": "git_auto_snapshot",
            "git_snapshot_on_start": "git_snapshot_on_start",
            "git_snapshot_on_recovery": "git_snapshot_on_recovery",
            "git_auto_push": "git_auto_push",
            "error_recovery": "error_recovery_enabled",
            "permission_auto_approve": "auto_approve_permission_requests",
        }
        if feature not in field_by_feature:
            return

        update_value = value_by_feature[feature]
        previous_value = not update_value
        update_field = field_by_feature[feature]
        active_targets = [
            provider for provider in targets
            if feature != "permission_auto_approve" or provider == "claude"
        ]
        if not active_targets:
            show_toast(self.winfo_toplevel(), "\u6743\u9650\u81ea\u52a8\u786e\u8ba4\u53ea\u9002\u7528\u4e8e Claude", is_error=True)
            self._refresh_remote_auto_switch_availability()
            return

        def worker():
            results = []
            failures = []
            updates = {update_field: update_value}
            for provider in active_targets:
                try:
                    results.append(
                        remote_auto_continue.update_remote_auto_continue_settings(
                            server_name,
                            provider,
                            updates,
                        )
                    )
                except Exception as e:
                    failures.append(f"{provider}: {e}")
            statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
            failures.extend(status_failures)
            return {"statuses": statuses, "failures": failures, "results": results}

        self._run_remote_auto_task(
            f"正在更新 {server_name} 的远端独立开关...",
            worker,
            lambda payload: self._finish_remote_auto_toggle(
                payload,
                feature,
                previous_value,
            ),
        )

    def _finish_remote_auto_toggle(self, payload, feature: str, previous_value: bool):
        self._show_remote_auto_result(payload, "\u8fdc\u7a0b\u5f00\u5173\u5df2\u66f4\u65b0", expect_ready=False)
        if payload.get("failures") and not self._remote_auto_statuses_cover_selection(payload.get("statuses", [])):
            cached = self._cached_remote_auto_statuses_for_selection()
            if cached:
                self._refresh_remote_auto_switches_from_statuses(cached)
            else:
                self._set_remote_auto_feature_var(feature, previous_value)
                self._refresh_remote_auto_switch_availability()

    def _check_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()

        def worker():
            statuses, failures = self._collect_remote_auto_statuses(server_name, targets)
            return {"statuses": statuses, "failures": failures, "results": []}

        self._run_remote_auto_task(
            f"正在检查 {server_name} 的远端自动续跑一致性...",
            worker,
            lambda payload: self._show_remote_auto_result(payload, "远端一致性检查完成", expect_ready=True),
        )

    def _install_remote_git_snapshot(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()

        def worker():
            results = []
            failures = []
            for provider in targets:
                try:
                    results.append(remote_auto_continue.install_remote_git_snapshot(server_name, provider))
                except Exception as e:
                    failures.append(f"{provider}: {e}")
            statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
            failures.extend(status_failures)
            return {"statuses": statuses, "failures": failures, "results": results}

        self._run_remote_auto_task(
            f"正在修复 {server_name} 的远端 Git 快照 Hook...",
            worker,
            lambda payload: self._show_remote_auto_result(payload, "远端 Git 快照 Hook 已修复", expect_ready=False),
        )

    def _install_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()

        def worker():
            results = []
            failures = []
            for provider in targets:
                try:
                    results.append(remote_auto_continue.install_remote_auto_continue(server_name, provider))
                except Exception as e:
                    failures.append(f"{provider}: {e}")
            statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
            failures.extend(status_failures)
            return {"statuses": statuses, "failures": failures, "results": results}

        self._run_remote_auto_task(
            f"正在一键修复 {server_name} 的远端自动续跑...",
            worker,
            lambda payload: self._show_remote_auto_result(payload, "远端自动续跑修复完成", expect_ready=True),
        )

    def _pause_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()

        def worker():
            results = []
            failures = []
            for provider in targets:
                try:
                    results.append(remote_auto_continue.pause_remote_auto_continue(server_name, provider))
                except Exception as e:
                    failures.append(f"{provider}: {e}")
            statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
            failures.extend(status_failures)
            return {"statuses": statuses, "failures": failures, "results": results}

        self._run_remote_auto_task(
            f"正在暂停 {server_name} 的远端 Stop 续跑...",
            worker,
            lambda payload: self._show_remote_auto_result(payload, "远端 Stop 续跑已暂停"),
        )

    def _uninstall_remote_auto_continue(self):
        server_name = self._selected_server_name()
        if not server_name:
            return

        targets = self._selected_remote_auto_targets()
        target_label = "、".join("Claude" if p == "claude" else "Codex" for p in targets)

        def do_uninstall():
            def worker():
                results = []
                failures = []
                for provider in targets:
                    try:
                        results.append(remote_auto_continue.uninstall_remote_auto_continue(server_name, provider))
                    except Exception as e:
                        failures.append(f"{provider}: {e}")
                statuses, status_failures = self._collect_remote_auto_statuses(server_name, targets)
                failures.extend(status_failures)
                return {"statuses": statuses, "failures": failures, "results": results}

            self._run_remote_auto_task(
                f"正在卸载 {server_name} 的远端自动续跑...",
                worker,
                lambda payload: self._show_remote_auto_result(payload, "远端自动续跑已卸载"),
            )

        ConfirmDialog(
            self.winfo_toplevel(),
            title="卸载远端自动续跑",
            message=f"确定要从服务器 \"{server_name}\" 卸载 {target_label} 自动续跑吗？\n这会移除远端 hook、脚本、设置和指导块。",
            on_confirm=do_uninstall,
        )

    def _inspect_remote_configs(self):
        server_name = self._server_combo.get()
        if not self._has_selected_server():
            show_toast(self.winfo_toplevel(), "\u8bf7\u5148\u9009\u62e9\u670d\u52a1\u5668", is_error=True)
            return
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return

        def done(payload):
            if not payload["ok"]:
                self._reset_remote_pull_options(f"读取远端配置失败: {payload['error']}")
                self._set_sync_status(f"读取远端配置失败: {payload['error']}", "error")
                show_toast(self.winfo_toplevel(), f"读取远端配置失败: {payload['error']}", is_error=True)
                return
            if self._server_combo.get() != server_name:
                message = "读取完成，但当前服务器已变化；请重新读取远端配置。"
                self._reset_remote_pull_options(message)
                self._set_sync_status(message, "warning")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            candidates = payload["result"]
            self._set_remote_pull_candidates(candidates, server_name)
            importable_count = len([candidate for candidate in candidates if candidate.importable])
            message = f"已读取 {server_name} 的远端配置，可拉取 {importable_count} 项"
            self._set_sync_status(message, "success" if importable_count else "warning")
            show_toast(self.winfo_toplevel(), message, is_error=not bool(importable_count))

        self._run_ssh_task(
            f"正在读取 {server_name} 上的远端配置...",
            lambda: sync_manager.inspect_remote_configs(server_name),
            on_done=done,
        )

    def _pull_from_server(self):
        server_name = self._server_combo.get()
        if not self._has_selected_server():
            show_toast(self.winfo_toplevel(), "\u8bf7\u5148\u9009\u62e9\u670d\u52a1\u5668", is_error=True)
            return
        if server_name == "(无)":
            show_toast(self.winfo_toplevel(), "请先选择服务器", is_error=True)
            return

        selected = self._remote_pull_combo.get() if self._remote_pull_combo else ""
        kinds = self._remote_pull_options.get(selected)
        if not kinds:
            show_toast(self.winfo_toplevel(), "请先读取远端配置，并选择可拉取的项目", is_error=True)
            self._set_sync_status("请先读取远端配置，并选择可拉取的项目", "warning")
            return
        if self._remote_pull_server_name != server_name:
            self._reset_remote_pull_options("服务器选择已变化，请重新读取远端配置。")
            show_toast(self.winfo_toplevel(), "服务器选择已变化，请重新读取远端配置", is_error=True)
            self._set_sync_status("服务器选择已变化，请重新读取远端配置", "warning")
            return

        def worker():
            results = []
            failures = []
            label_by_kind = {
                "claude": "Claude API",
                "claude_api": "Claude API",
                "claude_account": "Claude 账号",
                "codex": "Codex API",
                "codex_api": "Codex API",
                "codex_account": "Codex 账号",
            }
            for kind in kinds:
                try:
                    results.append(sync_manager.pull_remote_config_from_server(server_name, kind))
                except Exception as e:
                    failures.append(f"{label_by_kind.get(kind, kind)}: {e}")
            return results, failures

        def done(payload):
            if not payload["ok"]:
                message = f"拉取失败: {payload['error']}"
                self._set_sync_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
                return

            results, failures = payload["result"]
            if results and failures:
                message = " | ".join(results) + " | 部分失败: " + "；".join(failures)
                self._set_sync_status(message, "warning")
                show_toast(self.winfo_toplevel(), message, is_error=True)
            elif results:
                message = " | ".join(results)
                self._set_sync_status(message, "success")
                show_toast(self.winfo_toplevel(), message)
            else:
                message = "拉取失败: " + "；".join(failures)
                self._set_sync_status(message, "error")
                show_toast(self.winfo_toplevel(), message, is_error=True)
            self.refresh()

        self._run_ssh_task(
            f"正在从 {server_name} 拉取 {selected}...",
            worker,
            on_done=done,
        )
