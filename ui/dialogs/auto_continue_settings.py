import customtkinter as ctk
from models.auto_continue import AutoContinueSettings
from ui.theme import COLORS, button_style, center_window, font, input_style, textbox_style


class AutoContinueSettingsDialog(ctk.CTkToplevel):
    """Dialog for configuring auto-continue settings."""

    def __init__(self, master, provider_name: str, settings: AutoContinueSettings, on_save=None):
        super().__init__(master)
        self.title(f"{provider_name} 自动续跑设置")
        self.geometry("660x780")
        self.minsize(560, 600)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()

        self.provider_name = provider_name
        self.settings = settings
        self._on_save = on_save

        self._build_ui()
        center_window(self, master)

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=15, pady=(15, 10))
        ctk.CTkLabel(
            header,
            text="自动续跑设置",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(anchor="w")

        # Scrollable content
        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS["secondary"],
            scrollbar_button_hover_color=COLORS["secondary_hover"],
        )
        scroll.pack(fill="both", expand=True, padx=15, pady=(0, 10))

        # Max continuations
        self._add_field(scroll, "最大续跑次数", "max_continuations", str(self.settings.max_continuations))

        # Continuation prompt
        prompt_label = ctk.CTkLabel(scroll, text="续跑提示语", text_color=COLORS["muted"], anchor="w", font=font(12))
        prompt_label.pack(fill="x", pady=(10, 2))
        self._prompt_text = ctk.CTkTextbox(scroll, height=80, **textbox_style())
        self._prompt_text.insert("1.0", self.settings.continuation_prompt)
        self._prompt_text.pack(fill="x", pady=(0, 10))

        # Conservative mode
        self._conservative_var = ctk.BooleanVar(value=self.settings.conservative_mode)
        conservative_switch = ctk.CTkSwitch(scroll, text="保守模式 (stop_hook_active=true 时直接允许停止)",
                                             variable=self._conservative_var,
                                             text_color=COLORS["text"],
                                             progress_color=COLORS["success"],
                                             button_color=COLORS["text"])
        conservative_switch.pack(anchor="w", pady=5)

        # Apply to subagents (Claude only)
        if self.provider_name.lower() == "claude":
            self._subagents_var = ctk.BooleanVar(value=self.settings.apply_to_subagents)
            subagents_switch = ctk.CTkSwitch(scroll, text="应用到 Subagent (注册 SubagentStop hook)",
                                              variable=self._subagents_var,
                                              text_color=COLORS["text"],
                                              progress_color=COLORS["success"],
                                              button_color=COLORS["text"])
            subagents_switch.pack(anchor="w", pady=5)

        # Error recovery section
        ctk.CTkLabel(scroll, text="错误自动恢复", text_color=COLORS["text"], font=font(14, "bold")).pack(
            anchor="w", pady=(15, 5))

        self._error_recovery_var = ctk.BooleanVar(value=self.settings.error_recovery_enabled)
        error_recovery_switch = ctk.CTkSwitch(
            scroll,
            text="启用错误自动恢复 (智能识别和处理 10 种 API 错误)",
            variable=self._error_recovery_var,
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        error_recovery_switch.pack(anchor="w", pady=5)

        self._add_field(scroll, "最大恢复次数", "max_error_recoveries", str(self.settings.max_error_recoveries))

        # Git版本管理section
        ctk.CTkLabel(scroll, text="Git 版本管理", text_color=COLORS["text"], font=font(14, "bold")).pack(
            anchor="w", pady=(15, 5))

        self._git_auto_snapshot_var = ctk.BooleanVar(value=self.settings.git_auto_snapshot)
        git_auto_switch = ctk.CTkSwitch(
            scroll,
            text="启用自动 Git 快照 (推荐)",
            variable=self._git_auto_snapshot_var,
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        git_auto_switch.pack(anchor="w", pady=5)

        self._git_snapshot_on_start_var = ctk.BooleanVar(value=self.settings.git_snapshot_on_start)
        git_start_switch = ctk.CTkSwitch(
            scroll,
            text="对话开始时创建快照",
            variable=self._git_snapshot_on_start_var,
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        git_start_switch.pack(anchor="w", pady=5, padx=(20, 0))

        self._git_snapshot_on_recovery_var = ctk.BooleanVar(value=self.settings.git_snapshot_on_recovery)
        git_recovery_switch = ctk.CTkSwitch(
            scroll,
            text="错误恢复前创建快照",
            variable=self._git_snapshot_on_recovery_var,
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        git_recovery_switch.pack(anchor="w", pady=5, padx=(20, 0))

        # Git help text
        git_help_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        git_help_frame.pack(fill="x", pady=(2, 10))

        git_help_text = ctk.CTkLabel(
            git_help_frame,
            text="自动为项目创建 Git 快照，方便回滚到任意版本。\n"
                 "如果项目不是 Git 仓库，会自动初始化。",
            font=font(11),
            text_color=COLORS["muted"],
            justify="left",
            anchor="w"
        )
        git_help_text.pack(anchor="w")

        # Help text
        help_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        help_frame.pack(fill="x", pady=(2, 10))

        help_text = ctk.CTkLabel(
            help_frame,
            text="支持的错误类型：\n"
                 "  • 内容超长 → 自动压缩并继续\n"
                 "  • 速率限制 → 等待后重试\n"
                 "  • 服务器繁忙/超时 → 指数退避重试\n"
                 "  • 认证/权限/配额 → 友好提示",
            font=font(11),
            text_color=COLORS["muted"],
            justify="left",
            anchor="w"
        )
        help_text.pack(anchor="w")

        # Patterns section
        ctk.CTkLabel(scroll, text="未完成模式 (正则表达式)", text_color=COLORS["text"], font=font(13, "bold")).pack(
            anchor="w", pady=(15, 5))
        self._incomplete_text = ctk.CTkTextbox(scroll, height=100, **textbox_style(monospace=True))
        self._incomplete_text.insert("1.0", "\n".join(self.settings.incomplete_patterns))
        self._incomplete_text.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(scroll, text="阻塞模式 (正则表达式)", text_color=COLORS["text"], font=font(13, "bold")).pack(
            anchor="w", pady=(5, 5))
        self._blocker_text = ctk.CTkTextbox(scroll, height=100, **textbox_style(monospace=True))
        self._blocker_text.insert("1.0", "\n".join(self.settings.blocker_patterns))
        self._blocker_text.pack(fill="x", pady=(0, 10))

        # Buttons
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=15, pady=(0, 15))
        self._error_label = ctk.CTkLabel(btn_frame, text="", text_color=COLORS["danger"], font=font(12))
        self._error_label.pack(side="left")
        ctk.CTkButton(btn_frame, text="取消", command=self.destroy, **button_style("secondary")).pack(side="right", padx=5)
        ctk.CTkButton(btn_frame, text="保存", command=self._save, **button_style("primary")).pack(side="right")

    def _add_field(self, parent, label, key, value):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(row, text=label, width=150, anchor="w", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        entry = ctk.CTkEntry(row, width=400, **input_style())
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True)
        setattr(self, f"_{key}_entry", entry)

    def _save(self):
        try:
            # Collect values
            max_cont = int(self._max_continuations_entry.get())
            max_recoveries = int(self._max_error_recoveries_entry.get())
            if max_cont < 0 or max_recoveries < 0:
                raise ValueError("次数不能为负数")
            prompt = self._prompt_text.get("1.0", "end").strip()
            conservative = self._conservative_var.get()
            error_recovery = self._error_recovery_var.get()

            # Git settings
            git_auto_snapshot = self._git_auto_snapshot_var.get()
            git_snapshot_on_start = self._git_snapshot_on_start_var.get()
            git_snapshot_on_recovery = self._git_snapshot_on_recovery_var.get()

            incomplete = [line.strip() for line in self._incomplete_text.get("1.0", "end").split("\n") if line.strip()]
            blocker = [line.strip() for line in self._blocker_text.get("1.0", "end").split("\n") if line.strip()]

            # Build new settings
            new_settings = AutoContinueSettings(
                enabled=self.settings.enabled,
                max_continuations=max_cont,
                continuation_prompt=prompt,
                conservative_mode=conservative,
                error_recovery_enabled=error_recovery,
                max_error_recoveries=max_recoveries,
                git_auto_snapshot=git_auto_snapshot,
                git_snapshot_on_start=git_snapshot_on_start,
                git_snapshot_on_recovery=git_snapshot_on_recovery,
                incomplete_patterns=incomplete,
                blocker_patterns=blocker,
            )

            if self.provider_name.lower() == "claude":
                new_settings.apply_to_subagents = self._subagents_var.get()

            if self._on_save:
                self._on_save(new_settings)

            self.destroy()
        except Exception as e:
            self._error_label.configure(text=f"保存失败: {e}")
