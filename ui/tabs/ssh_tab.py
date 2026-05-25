import threading
import customtkinter as ctk
from ui.widgets.empty_state import EmptyState
from ui.widgets.toast import show_toast
from ui.dialogs.ssh_editor import SSHEditorDialog
from ui.dialogs.confirm_dialog import ConfirmDialog
from core import profile_manager, ssh_manager, sync_manager, remote_auto_continue, remote_git_login
from core.auto_continue.manager import auto_continue_manager
from models.auto_continue import training_prompt_template_by_key
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font


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
            text="批量目标: 未勾选（使用下方单台目标）",
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._batch_target_label.pack(side="left", padx=(12, 0))
        ctk.CTkFrame(sync_header, fg_color="transparent").pack(side="left", fill="x", expand=True)
        self._batch_select_all_button = ctk.CTkButton(
            sync_header,
            text="全选服务器",
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

        # Server selector
        ctk.CTkLabel(
            sync_controls,
            text="目标服务器",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self._server_combo = ctk.CTkComboBox(
            sync_controls,
            values=["(无)"],
            width=220,
            command=lambda _value: self._on_server_selection_change(),
            **combo_style(),
        )
        self._server_combo.grid(row=0, column=1, sticky="ew", padx=(8, 12))

        ctk.CTkButton(
            sync_controls,
            text="推送当前生效",
            width=126,
            command=self._sync_current,
            **button_style("primary"),
        ).grid(row=0, column=2, sticky="e", padx=(0, 8))
        self._remote_inspect_button = ctk.CTkButton(
            sync_controls,
            text="读取远端配置",
            width=126,
            command=self._inspect_remote_configs,
            **button_style("accent"),
        )
        self._remote_inspect_button.grid(row=0, column=3, sticky="e")

        ctk.CTkLabel(
            sync_controls,
            text="推送内容",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._sync_kind_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._sync_kind_options.keys()),
            width=132,
            command=lambda _value: self._on_sync_kind_change(),
            **combo_style(),
        )
        self._sync_kind_combo.grid(row=1, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._sync_kind_combo.set("Claude API")

        self._profile_combo = ctk.CTkComboBox(
            sync_controls,
            values=["(无)"],
            width=220,
            **combo_style(),
        )
        self._profile_combo.grid(row=1, column=2, sticky="ew", padx=(0, 8), pady=(10, 0))

        ctk.CTkButton(
            sync_controls,
            text="推送所选",
            width=126,
            command=self._sync_selected,
            **button_style("primary"),
        ).grid(row=1, column=3, sticky="e", pady=(10, 0))

        ctk.CTkLabel(
            sync_controls,
            text="远端拉取",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self._remote_pull_type_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._remote_pull_type_options.keys()),
            width=132,
            command=lambda _value: self._refresh_remote_pull_combo(),
            state="disabled",
            **combo_style(),
        )
        self._remote_pull_type_combo.grid(row=2, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._remote_pull_type_combo.set("全部项目")
        self._remote_pull_combo = ctk.CTkComboBox(
            sync_controls,
            values=["请先读取远端配置"],
            width=220,
            state="disabled",
            **combo_style(),
        )
        self._remote_pull_combo.grid(row=2, column=2, sticky="ew", padx=(0, 8), pady=(10, 0))
        self._remote_pull_combo.set("请先读取远端配置")
        self._remote_pull_button = ctk.CTkButton(
            sync_controls,
            text="拉取所选",
            width=126,
            command=self._pull_from_server,
            state="disabled",
            **button_style("accent"),
        )
        self._remote_pull_button.grid(row=2, column=3, sticky="e", pady=(10, 0))
        self._remote_pull_hint = ctk.CTkLabel(
            sync_controls,
            text="先读取服务器上实际存在的配置；随后可按 API/账号或 Claude/Codex 过滤，并选择全部或具体项目拉取到本机。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._remote_pull_hint.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        bind_wraplength(sync_controls, self._remote_pull_hint, padding=20)

        ctk.CTkLabel(
            sync_controls,
            text="Codex Wire API",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=4, column=0, sticky="w", pady=(10, 0))
        self._codex_wire_api_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._codex_wire_api_options.keys()),
            width=160,
            **combo_style(),
        )
        self._codex_wire_api_combo.grid(row=4, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._codex_wire_api_combo.set("远端自测选择")
        self._codex_wire_api_hint = ctk.CTkLabel(
            sync_controls,
            text="推送 Codex API 时生效；远端自测会在服务器上各跑 3 次并回写最稳选项",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._codex_wire_api_hint.grid(row=4, column=2, columnspan=2, sticky="ew", pady=(10, 0))
        bind_wraplength(sync_controls, self._codex_wire_api_hint, padding=20)

        ctk.CTkLabel(
            sync_controls,
            text="远端清理",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=5, column=0, sticky="w", pady=(10, 0))
        self._clear_api_combo = ctk.CTkComboBox(
            sync_controls,
            values=list(self._clear_api_options.keys()),
            width=160,
            **combo_style(),
        )
        self._clear_api_combo.grid(row=5, column=1, sticky="w", padx=(8, 12), pady=(10, 0))
        self._clear_api_combo.set("Claude + Codex")
        clear_hint = ctk.CTkLabel(
            sync_controls,
            text="移除服务器当前 API Key/Token、Base URL 覆盖和本工具写入的相关远端环境变量。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        clear_hint.grid(row=5, column=2, sticky="ew", pady=(10, 0))
        bind_wraplength(sync_controls, clear_hint, padding=20)
        ctk.CTkButton(
            sync_controls,
            text="清除远端 API",
            width=126,
            command=self._clear_remote_api_info,
            **button_style("danger"),
        ).grid(row=5, column=3, sticky="e", pady=(10, 0))

        ctk.CTkLabel(
            sync_controls,
            text="Git 登录",
            text_color=COLORS["muted"],
            width=82,
            anchor="w",
        ).grid(row=6, column=0, sticky="w", pady=(10, 0))
        self._git_login_status_label = ctk.CTkLabel(
            sync_controls,
            text="检查本机/远端 Git 身份和 GitHub CLI 登录；可同步 gh token，无法读取 Windows 凭据库内部密码。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._git_login_status_label.grid(row=6, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=(10, 0))
        bind_wraplength(sync_controls, self._git_login_status_label, padding=20)
        git_btn_frame = ctk.CTkFrame(sync_controls, fg_color="transparent")
        git_btn_frame.grid(row=6, column=3, sticky="e", pady=(10, 0))
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
        self._sync_status_label.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        bind_wraplength(sync_controls, self._sync_status_label, padding=20)

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
                    text="",
                    width=20,
                    checkbox_width=18,
                    checkbox_height=18,
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
        self._update_batch_target_label(server_names)
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

    def _update_batch_target_label(self, server_names: list[str] | None = None):
        all_names = server_names if server_names is not None else self._profile_server_names()
        self._selected_server_names.intersection_update(all_names)
        selected = [name for name in all_names if name in self._selected_server_names]
        if self._batch_target_label:
            if selected:
                self._batch_target_label.configure(
                    text=f"批量目标: {self._format_server_target(selected)}",
                    text_color=COLORS["accent"],
                )
            else:
                self._batch_target_label.configure(
                    text="批量目标: 未勾选（使用下方单台目标）",
                    text_color=COLORS["muted"],
                )
        action_state = "normal" if all_names else "disabled"
        for button in (self._batch_select_all_button, self._batch_clear_button):
            if button:
                try:
                    button.configure(state=action_state)
                except Exception:
                    pass

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
                results.append(f"{server_name}: {result}")
            except Exception as e:
                failures.append(f"{server_name}: {e}")
        return {"results": results, "failures": failures, "server_names": server_names}

    def _show_server_batch_result(self, payload, success_message: str):
        if not payload["ok"]:
            message = f"批量操作失败: {payload['error']}"
            self._set_sync_status(message, "error")
            show_toast(self.winfo_toplevel(), message, is_error=True)
            return

        result = payload.get("result") or {}
        results = result.get("results", [])
        failures = result.get("failures", [])
        if failures and results:
            message = " | ".join(results) + " | 部分失败: " + "；".join(failures)
            severity = "warning"
        elif failures:
            message = "批量操作失败: " + "；".join(failures)
            severity = "error"
        else:
            message = " | ".join(results) if results else success_message
            severity = "success"
        self._set_sync_status(message, severity)
        show_toast(self.winfo_toplevel(), message, is_error=bool(failures))

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
                or "先读取服务器上实际存在的配置；随后可按 API/账号或 Claude/Codex 过滤，并选择全部或具体项目拉取到本机。",
                text_color=COLORS["muted"],
            )

    def _on_server_selection_change(self):
        self._reset_remote_pull_options()
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
                text="先读取服务器上实际存在的配置；随后可按 API/账号或 Claude/Codex 过滤，并选择全部或具体项目拉取到本机。",
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
