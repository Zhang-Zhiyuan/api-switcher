import customtkinter as ctk
from models.auto_continue import (
    DEFAULT_TRAINING_CONTINUE_PROMPT,
    TRAINING_PROMPT_TEMPLATES,
    AutoContinueSettings,
    training_prompt_template_by_key,
    training_prompt_template_by_name,
)
from ui.theme import (
    COLORS,
    bind_wraplength,
    button_style,
    center_window,
    combo_style,
    font,
    input_style,
    textbox_style,
)


def _auto_continue_settings_layout(width: int) -> tuple[bool, int]:
    """Return field/template stacking and template action columns."""
    available = max(1, int(width))
    stacked = available < 650
    action_columns = 3 if available >= 420 else (2 if available >= 300 else 1)
    return stacked, action_columns


class AutoContinueSettingsDialog(ctk.CTkToplevel):
    """Dialog for configuring auto-continue settings."""

    def __init__(self, master, provider_name: str, settings: AutoContinueSettings, on_save=None):
        super().__init__(master)
        self.title(f"{provider_name} 自动续跑设置")
        self.geometry("780x860")
        self.minsize(400, 560)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()

        self.provider_name = provider_name
        self.settings = settings
        self._on_save = on_save
        self._responsive_rows = []
        self._responsive_after_id = None
        self._responsive_state = None

        self._build_ui()
        self.bind("<Configure>", self._schedule_responsive_layout, add="+")
        self._schedule_responsive_layout(delay_ms=0)
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
        header_description = ctk.CTkLabel(
            header,
            text="Stop 续跑、训练守护、Git 快照、API 恢复和权限自动确认分别独立控制。",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        header_description.pack(fill="x", anchor="w", pady=(2, 0))
        bind_wraplength(header, header_description, padding=4, min_width=220, max_width=740)

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

        self._training_template_row = ctk.CTkFrame(scroll, fg_color="transparent")
        self._training_template_row.pack(fill="x", pady=(8, 2))
        self._training_template_label = ctk.CTkLabel(
            self._training_template_row,
            text="Prompt 模板",
            text_color=COLORS["muted"],
            anchor="w",
            font=font(12),
            width=86,
        )
        selected_template = training_prompt_template_by_key(self.settings.training_prompt_template_key)
        self._training_template_var = ctk.StringVar(value=selected_template["name"])
        template_names = [template["name"] for template in TRAINING_PROMPT_TEMPLATES]
        self._training_template_combo = ctk.CTkComboBox(
            self._training_template_row,
            variable=self._training_template_var,
            values=template_names,
            command=self._on_training_template_selected,
            width=210,
            **combo_style(),
        )
        apply_template_button = ctk.CTkButton(
            self._training_template_row,
            text="应用",
            width=58,
            command=lambda: self._apply_training_template(append=False),
            **button_style("accent", compact=True),
        )
        append_template_button = ctk.CTkButton(
            self._training_template_row,
            text="追加",
            width=58,
            command=lambda: self._apply_training_template(append=True),
            **button_style("secondary", compact=True),
        )
        restore_template_button = ctk.CTkButton(
            self._training_template_row,
            text="恢复默认",
            width=86,
            command=self._restore_default_training_prompt,
            **button_style("secondary", compact=True),
        )
        self._training_template_buttons = [
            apply_template_button,
            append_template_button,
            restore_template_button,
        ]
        self._training_template_row.grid_columnconfigure(1, weight=1)
        self._training_template_label.grid(row=0, column=0, sticky="w")
        self._training_template_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        for index, button in enumerate(self._training_template_buttons, start=2):
            button.grid(row=0, column=index, sticky="ew", padx=(0 if index == 2 else 6, 0))
        self._training_template_desc = ctk.CTkLabel(
            scroll,
            text=selected_template["description"],
            text_color=COLORS["muted_soft"],
            anchor="w",
            justify="left",
            font=font(11),
        )
        self._training_template_desc.pack(fill="x", pady=(0, 6))
        bind_wraplength(scroll, self._training_template_desc, padding=8, min_width=220, max_width=740)

        training_prompt_description = ctk.CTkLabel(
            scroll,
            text="训练目标/续跑指令（可手写，也可先套模板再改指标）",
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
            font=font(12),
        )
        training_prompt_description.pack(fill="x", pady=(10, 2))
        bind_wraplength(scroll, training_prompt_description, padding=8, min_width=220, max_width=740)
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
            permission_tools_description = ctk.CTkLabel(
                scroll,
                text="自动确认工具（每行一个，支持 * 通配；默认包含 Bash/Edit/MultiEdit/Write/NotebookEdit）",
                text_color=COLORS["muted"],
                anchor="w",
                justify="left",
                font=font(12),
            )
            permission_tools_description.pack(fill="x", pady=(10, 2))
            bind_wraplength(scroll, permission_tools_description, padding=8, min_width=220, max_width=740)
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
            "支持：内容超长自动压缩、429 按 Retry-After 等待、断联/超时/服务繁忙指数退避、"
            "认证/权限/配额友好提示。Codex 自动压缩失败会按退避间隔持续重试直到成功，"
            "不受最大恢复次数限制。",
        )

        self._add_section(scroll, "Git 快照", "自动创建本地 Git 快照，方便恢复到手动任务、续跑或错误恢复前后的状态。")

        self._git_auto_snapshot_var = ctk.BooleanVar(value=self.settings.git_auto_snapshot)
        self._git_auto_snapshot_switch = self._add_switch(scroll, "启用自动 Git 快照 (推荐)", self._git_auto_snapshot_var)
        self._git_auto_snapshot_switch.configure(command=self._refresh_git_snapshot_children)

        self._git_snapshot_on_start_var = ctk.BooleanVar(value=self.settings.git_snapshot_on_start)
        self._git_snapshot_on_start_switch = self._add_switch(
            scroll,
            "开新对话/发消息/Stop 时创建快照",
            self._git_snapshot_on_start_var,
            padx=(20, 0),
        )

        self._git_snapshot_on_recovery_var = ctk.BooleanVar(value=self.settings.git_snapshot_on_recovery)
        self._git_snapshot_on_recovery_switch = self._add_switch(
            scroll,
            "API 错误恢复前创建快照",
            self._git_snapshot_on_recovery_var,
            padx=(20, 0),
        )
        self._git_auto_push_var = ctk.BooleanVar(value=self.settings.git_auto_push)
        self._git_auto_push_switch = self._add_switch(
            scroll,
            "快照提交后推送已有 Git remote/upstream",
            self._git_auto_push_var,
            padx=(20, 0),
        )
        self._refresh_git_snapshot_children()

        # Git help text
        git_help_frame = ctk.CTkFrame(scroll, fg_color="transparent")
        git_help_frame.pack(fill="x", pady=(2, 10))

        git_help_text = ctk.CTkLabel(
            git_help_frame,
            text="自动为项目创建本地 Git 快照，方便回滚到任意版本。\n"
                 "已有仓库会保留 remote 和实名身份；没有仓库时才自动初始化。\n"
                 "自动推送仅在仓库已有远端或上游分支时尝试；失败只记录日志，不阻断续跑。",
            font=font(11),
            text_color=COLORS["muted"],
            justify="left",
            anchor="w"
        )
        git_help_text.pack(anchor="w")
        bind_wraplength(git_help_frame, git_help_text, padding=4, min_width=220, max_width=740)

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
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=15, pady=(0, 15))
        self._error_label = ctk.CTkLabel(
            footer,
            text="",
            text_color=COLORS["danger"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self._error_label.pack(fill="x", pady=(0, 5))
        bind_wraplength(footer, self._error_label, padding=4, min_width=220, max_width=740)
        button_row = ctk.CTkFrame(footer, fg_color="transparent")
        button_row.pack(fill="x")
        ctk.CTkButton(button_row, text="取消", command=self.destroy, **button_style("secondary")).pack(
            side="right", padx=(5, 0)
        )
        ctk.CTkButton(button_row, text="保存", command=self._save, **button_style("primary")).pack(side="right")

    def _add_section(self, parent, title, subtitle):
        ctk.CTkLabel(
            parent,
            text=title,
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(anchor="w", pady=(16, 2))
        subtitle_label = ctk.CTkLabel(
            parent,
            text=subtitle,
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle_label.pack(fill="x", pady=(0, 6))
        bind_wraplength(parent, subtitle_label, padding=8, min_width=220, max_width=740)

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

    def _refresh_git_snapshot_children(self):
        state = "normal" if bool(self._git_auto_snapshot_var.get()) else "disabled"
        for switch in (
            getattr(self, "_git_snapshot_on_start_switch", None),
            getattr(self, "_git_snapshot_on_recovery_switch", None),
            getattr(self, "_git_auto_push_switch", None),
        ):
            if switch is not None:
                switch.configure(state=state)

    def _add_note(self, parent, text):
        note = ctk.CTkLabel(
            parent,
            text=text,
            text_color=COLORS["muted"],
            font=font(11),
            anchor="w",
            justify="left",
        )
        note.pack(fill="x", pady=(2, 10))
        bind_wraplength(parent, note, padding=8, min_width=220, max_width=740)

    def _add_field(self, parent, label, key, value):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=5)
        label_widget = ctk.CTkLabel(
            row,
            text=label,
            width=190,
            anchor="w",
            text_color=COLORS["muted"],
            font=font(12),
        )
        label_widget.pack(side="left")
        entry = ctk.CTkEntry(row, width=400, **input_style())
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True)
        self._responsive_rows.append((label_widget, entry))
        setattr(self, f"_{key}_entry", entry)

    def _logical_width(self) -> int:
        width = self.winfo_width()
        try:
            scaling = float(self._get_window_scaling())
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
            self._responsive_after_id = self.after_idle(apply_layout) if delay_ms <= 0 else self.after(
                delay_ms, apply_layout
            )
        except Exception:
            self._responsive_after_id = None

    def _apply_responsive_layout(self) -> None:
        stacked, action_columns = _auto_continue_settings_layout(self._logical_width())
        state = (stacked, action_columns)
        if state == self._responsive_state:
            return
        self._responsive_state = state

        for label, entry in self._responsive_rows:
            label.pack_forget()
            entry.pack_forget()
            if stacked:
                label.configure(width=0)
                entry.configure(width=1)
                label.pack(side="top", fill="x", anchor="w")
                entry.pack(side="top", fill="x", expand=True, pady=(3, 0))
            else:
                label.configure(width=190)
                entry.configure(width=400)
                label.pack(side="left")
                entry.pack(side="left", fill="x", expand=True)

        template_widgets = (
            self._training_template_label,
            self._training_template_combo,
            *self._training_template_buttons,
        )
        for widget in template_widgets:
            widget.grid_forget()
        for column in range(5):
            self._training_template_row.grid_columnconfigure(column, weight=0, minsize=0, uniform="")

        if stacked:
            for column in range(action_columns):
                self._training_template_row.grid_columnconfigure(
                    column,
                    weight=1,
                    uniform="auto-continue-template-actions",
                )
            self._training_template_label.configure(width=0)
            self._training_template_combo.configure(width=1)
            self._training_template_label.grid(
                row=0,
                column=0,
                columnspan=action_columns,
                sticky="ew",
            )
            self._training_template_combo.grid(
                row=1,
                column=0,
                columnspan=action_columns,
                sticky="ew",
                pady=(3, 6),
            )
            for index, button in enumerate(self._training_template_buttons):
                column = index % action_columns
                button.grid(
                    row=2 + index // action_columns,
                    column=column,
                    sticky="ew",
                    padx=(0 if column == 0 else 6, 0),
                    pady=(0 if index < action_columns else 6, 0),
                )
        else:
            self._training_template_row.grid_columnconfigure(1, weight=1)
            self._training_template_label.configure(width=86)
            self._training_template_combo.configure(width=210)
            self._training_template_label.grid(row=0, column=0, sticky="w")
            self._training_template_combo.grid(row=0, column=1, sticky="ew", padx=(0, 8))
            for index, button in enumerate(self._training_template_buttons, start=2):
                button.grid(row=0, column=index, sticky="ew", padx=(0 if index == 2 else 6, 0))

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
            git_auto_push = self._git_auto_push_var.get()
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
                git_auto_push=git_auto_push,
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
