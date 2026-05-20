import customtkinter as ctk
from models.auto_continue import (
    DEFAULT_TRAINING_CONTINUE_PROMPT,
    TRAINING_PROMPT_TEMPLATES,
    AutoContinueSettings,
    training_prompt_template_by_key,
    training_prompt_template_by_name,
)
from ui.theme import COLORS, button_style, center_window, combo_style, font, input_style, textbox_style


class AutoContinueSettingsDialog(ctk.CTkToplevel):
    """Dialog for configuring auto-continue settings."""

    def __init__(self, master, provider_name: str, settings: AutoContinueSettings, on_save=None):
        super().__init__(master)
        self.title(f"{provider_name} 自动续跑设置")
        self.geometry("780x860")
        self.minsize(680, 680)
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
            text="自动续跑与 Hook 设置",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            header,
            text="Stop 续跑、训练守护、Git 快照、API 恢复和权限自动确认分别独立控制。",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        # Scrollable content
        scroll = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            scrollbar_button_color=COLORS["secondary"],
            scrollbar_button_hover_color=COLORS["secondary_hover"],
        )
        scroll.pack(fill="both", expand=True, padx=15, pady=(0, 10))

        self._add_section(scroll, "Stop 续跑", "控制 Stop hook 是否拦截未完成回答并自动续跑。")

        self._enabled_var = ctk.BooleanVar(value=self.settings.enabled)
        self._add_switch(scroll, "启用 Stop 自动续跑", self._enabled_var)

        self._conservative_var = ctk.BooleanVar(value=self.settings.conservative_mode)
        self._add_switch(scroll, "保守模式：hook 已在续跑时允许停止", self._conservative_var)

        if self.provider_name.lower() == "claude":
            self._subagents_var = ctk.BooleanVar(value=self.settings.apply_to_subagents)
            self._add_switch(scroll, "应用到 Claude Subagent (SubagentStop hook)", self._subagents_var)

        self._add_field(scroll, "最大续跑次数 (-1=不限)", "max_continuations", str(self.settings.max_continuations))

        # Continuation prompt
        prompt_label = ctk.CTkLabel(scroll, text="续跑提示语", text_color=COLORS["muted"], anchor="w", font=font(12))
        prompt_label.pack(fill="x", pady=(10, 2))
        self._prompt_text = ctk.CTkTextbox(scroll, height=80, **textbox_style())
        self._prompt_text.insert("1.0", self.settings.continuation_prompt)
        self._prompt_text.pack(fill="x", pady=(0, 10))

        self._add_section(
            scroll,
            "深度学习续跑",
            "独立的 Stop hook 守护；让 Codex/Claude 检查训练评估结果，未达标就继续训练或改进模型。",
        )
        self._training_auto_continue_var = ctk.BooleanVar(value=self.settings.training_auto_continue_enabled)
        self._add_switch(
            scroll,
            "启用训练续跑守护",
            self._training_auto_continue_var,
            progress_color=COLORS["accent"],
        )

        template_row = ctk.CTkFrame(scroll, fg_color="transparent")
        template_row.pack(fill="x", pady=(8, 2))
        ctk.CTkLabel(
            template_row,
            text="Prompt 模板",
            text_color=COLORS["muted"],
            anchor="w",
            font=font(12),
            width=86,
        ).pack(side="left")
        selected_template = training_prompt_template_by_key(self.settings.training_prompt_template_key)
        self._training_template_var = ctk.StringVar(value=selected_template["name"])
        template_names = [template["name"] for template in TRAINING_PROMPT_TEMPLATES]
        self._training_template_combo = ctk.CTkComboBox(
            template_row,
            variable=self._training_template_var,
            values=template_names,
            command=self._on_training_template_selected,
            width=210,
            **combo_style(),
        )
        self._training_template_combo.pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            template_row,
            text="应用",
            width=58,
            command=lambda: self._apply_training_template(append=False),
            **button_style("accent", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            template_row,
            text="追加",
            width=58,
            command=lambda: self._apply_training_template(append=True),
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            template_row,
            text="恢复默认",
            width=86,
            command=self._restore_default_training_prompt,
            **button_style("secondary", compact=True),
        ).pack(side="left")
        self._training_template_desc = ctk.CTkLabel(
            scroll,
            text=selected_template["description"],
            text_color=COLORS["muted_soft"],
            anchor="w",
            justify="left",
            font=font(11),
        )
        self._training_template_desc.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(
            scroll,
            text="训练目标/续跑指令（可手写，也可先套模板再改指标）",
            text_color=COLORS["muted"],
            anchor="w",
            font=font(12),
        ).pack(fill="x", pady=(10, 2))
        self._training_prompt_text = ctk.CTkTextbox(scroll, height=150, **textbox_style())
        self._training_prompt_text.insert("1.0", self.settings.training_continue_prompt)
        self._training_prompt_text.pack(fill="x", pady=(0, 6))
        self._add_note(
            scroll,
            "达标时写出 TRAINING_TARGET_MET 会停止续跑；非训练任务可写 TRAINING_NOT_APPLICABLE 跳过训练守护。",
        )

        if self.provider_name.lower() == "claude":
            self._add_section(scroll, "Claude 权限自动确认", "自动处理 Claude Code 的 PermissionRequest / PreToolUse。")
            self._permission_auto_approve_var = ctk.BooleanVar(value=self.settings.auto_approve_permission_requests)
            self._add_switch(
                scroll,
                "自动允许配置内的权限询问",
                self._permission_auto_approve_var,
                progress_color=COLORS["warning"],
            )
            self._add_field(
                scroll,
                "自动确认最大次数 (0=一直，推荐)",
                "auto_approve_max_per_session",
                str(self.settings.auto_approve_max_per_session),
            )
            ctk.CTkLabel(
                scroll,
                text="自动确认工具（每行一个，支持 * 通配；默认包含 Bash/Edit/MultiEdit/Write/NotebookEdit）",
                text_color=COLORS["muted"],
                anchor="w",
                font=font(12),
            ).pack(fill="x", pady=(10, 2))
            self._auto_approve_tools_text = ctk.CTkTextbox(scroll, height=70, **textbox_style(monospace=True))
            self._auto_approve_tools_text.insert("1.0", "\n".join(self.settings.auto_approve_tools))
            self._auto_approve_tools_text.pack(fill="x", pady=(0, 10))

        self._add_section(scroll, "API 错误恢复", "处理断联、超时、429、服务端错误和上下文过长。")

        self._error_recovery_var = ctk.BooleanVar(value=self.settings.error_recovery_enabled)
        self._add_switch(scroll, "启用 API 错误自动恢复 (Error / ResponseError hook)", self._error_recovery_var)

        self._add_field(scroll, "最大恢复次数", "max_error_recoveries", str(self.settings.max_error_recoveries))
        self._add_field(
            scroll,
            "断联初始重试间隔(秒)",
            "error_retry_initial_delay_seconds",
            str(self.settings.error_retry_initial_delay_seconds),
        )
        self._add_field(
            scroll,
            "断联最大重试间隔(秒)",
            "error_retry_max_delay_seconds",
            str(self.settings.error_retry_max_delay_seconds),
        )
        self._add_note(
            scroll,
            "支持：内容超长自动压缩、429 按 Retry-After 等待、断联/超时/服务繁忙指数退避、认证/权限/配额友好提示。",
        )

        self._add_section(scroll, "Git 快照", "自动创建本地 Git 快照，方便恢复到手动任务、续跑或错误恢复前后的状态。")

        self._git_auto_snapshot_var = ctk.BooleanVar(value=self.settings.git_auto_snapshot)
        self._add_switch(scroll, "启用自动 Git 快照 (推荐)", self._git_auto_snapshot_var)

        self._git_snapshot_on_start_var = ctk.BooleanVar(value=self.settings.git_snapshot_on_start)
        self._add_switch(scroll, "开新对话/发消息/Stop 时创建快照", self._git_snapshot_on_start_var, padx=(20, 0))

        self._git_snapshot_on_recovery_var = ctk.BooleanVar(value=self.settings.git_snapshot_on_recovery)
        self._add_switch(scroll, "API 错误恢复前创建快照", self._git_snapshot_on_recovery_var, padx=(20, 0))

        # Git help text
        git_help_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        git_help_frame.pack(fill="x", pady=(2, 10))

        git_help_text = ctk.CTkLabel(
            git_help_frame,
            text="自动为项目创建本地 Git 快照，方便回滚到任意版本。\n"
                 "已有仓库会保留 remote 和实名身份；没有仓库时才自动初始化。",
            font=font(11),
            text_color=COLORS["muted"],
            justify="left",
            anchor="w"
        )
        git_help_text.pack(anchor="w")

        self._add_section(scroll, "识别规则", "中英文正则规则；未完成会继续，阻塞会停止并等待用户。")
        ctk.CTkLabel(scroll, text="未完成模式 (正则表达式)", text_color=COLORS["text"], font=font(13, "bold")).pack(
            anchor="w", pady=(6, 5))
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

    def _add_section(self, parent, title, subtitle):
        ctk.CTkLabel(
            parent,
            text=title,
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(anchor="w", pady=(16, 2))
        ctk.CTkLabel(
            parent,
            text=subtitle,
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(0, 6))

    def _add_switch(self, parent, text, variable, progress_color=None, padx=(0, 0)):
        switch = ctk.CTkSwitch(
            parent,
            text=text,
            variable=variable,
            text_color=COLORS["text"],
            progress_color=progress_color or COLORS["success"],
            button_color=COLORS["text"],
        )
        switch.pack(anchor="w", pady=5, padx=padx)
        return switch

    def _add_note(self, parent, text):
        ctk.CTkLabel(
            parent,
            text=text,
            text_color=COLORS["muted"],
            font=font(11),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(2, 10))

    def _add_field(self, parent, label, key, value):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        ctk.CTkLabel(row, text=label, width=190, anchor="w", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        entry = ctk.CTkEntry(row, width=400, **input_style())
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True)
        setattr(self, f"_{key}_entry", entry)

    def _selected_training_template(self) -> dict[str, str]:
        name = self._training_template_var.get()
        return training_prompt_template_by_name(name)

    def _set_training_prompt_text(self, text: str) -> None:
        self._training_prompt_text.delete("1.0", "end")
        self._training_prompt_text.insert("1.0", text.strip())

    def _on_training_template_selected(self, _value=None):
        template = self._selected_training_template()
        self._training_template_desc.configure(text=template["description"])

    def _apply_training_template(self, append: bool = False):
        template_prompt = self._selected_training_template()["prompt"].strip()
        if append:
            current = self._training_prompt_text.get("1.0", "end").strip()
            next_prompt = f"{current}\n\n---\n{template_prompt}" if current else template_prompt
        else:
            next_prompt = template_prompt
        self._set_training_prompt_text(next_prompt)
        self._training_auto_continue_var.set(True)
        self._error_label.configure(text="")

    def _restore_default_training_prompt(self):
        self._set_training_prompt_text(DEFAULT_TRAINING_CONTINUE_PROMPT)
        self._training_template_var.set(TRAINING_PROMPT_TEMPLATES[0]["name"])
        self._on_training_template_selected()
        self._error_label.configure(text="")

    def _save(self):
        try:
            # Collect values
            max_cont = int(self._max_continuations_entry.get())
            max_recoveries = int(self._max_error_recoveries_entry.get())
            retry_initial_delay = int(self._error_retry_initial_delay_seconds_entry.get())
            retry_max_delay = int(self._error_retry_max_delay_seconds_entry.get())
            if max_cont < -1 or max_recoveries < 0:
                raise ValueError("续跑次数必须为 -1 或非负数；恢复次数不能为负数")
            enabled = self._enabled_var.get()
            prompt = self._prompt_text.get("1.0", "end").strip()
            training_auto_continue = self._training_auto_continue_var.get()
            training_prompt = self._training_prompt_text.get("1.0", "end").strip()
            training_template_key = self._selected_training_template()["key"]
            conservative = self._conservative_var.get()
            error_recovery = self._error_recovery_var.get()

            # Git settings
            git_auto_snapshot = self._git_auto_snapshot_var.get()
            git_snapshot_on_start = self._git_snapshot_on_start_var.get()
            git_snapshot_on_recovery = self._git_snapshot_on_recovery_var.get()

            auto_approve_permission_requests = self.settings.auto_approve_permission_requests
            auto_approve_max = self.settings.auto_approve_max_per_session
            auto_approve_bash = self.settings.auto_approve_bash
            auto_approve_tools = list(self.settings.auto_approve_tools)
            if hasattr(self, "_permission_auto_approve_var"):
                auto_approve_permission_requests = self._permission_auto_approve_var.get()
                auto_approve_max = int(self._auto_approve_max_per_session_entry.get())
                auto_approve_tools = [
                    line.strip()
                    for line in self._auto_approve_tools_text.get("1.0", "end").replace(",", "\n").split("\n")
                    if line.strip()
                ]
                auto_approve_bash = any(tool.casefold() == "bash" for tool in auto_approve_tools)

            incomplete = [line.strip() for line in self._incomplete_text.get("1.0", "end").split("\n") if line.strip()]
            blocker = [line.strip() for line in self._blocker_text.get("1.0", "end").split("\n") if line.strip()]

            # Build new settings
            new_settings = AutoContinueSettings(
                enabled=enabled,
                max_continuations=max_cont,
                continuation_prompt=prompt,
                conservative_mode=conservative,
                training_auto_continue_enabled=training_auto_continue,
                training_prompt_template_key=training_template_key,
                training_continue_prompt=training_prompt,
                error_recovery_enabled=error_recovery,
                max_error_recoveries=max_recoveries,
                error_retry_initial_delay_seconds=retry_initial_delay,
                error_retry_max_delay_seconds=retry_max_delay,
                git_auto_snapshot=git_auto_snapshot,
                git_snapshot_on_start=git_snapshot_on_start,
                git_snapshot_on_recovery=git_snapshot_on_recovery,
                auto_approve_permission_requests=auto_approve_permission_requests,
                auto_approve_max_per_session=auto_approve_max,
                auto_approve_bash=auto_approve_bash,
                auto_approve_tools=auto_approve_tools,
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
