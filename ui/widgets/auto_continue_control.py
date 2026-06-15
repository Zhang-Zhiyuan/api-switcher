from __future__ import annotations

import threading

import customtkinter as ctk
from core.lazy_imports import LazyAttribute
from ui.widgets.toast import show_toast
from ui.theme import COLORS, button_style, card_frame_kwargs, font, textbox_style


AutoContinueSettings = LazyAttribute("models.auto_continue", "AutoContinueSettings")
training_prompt_template_by_key = LazyAttribute("models.auto_continue", "training_prompt_template_by_key")
auto_continue_manager = LazyAttribute("core.auto_continue.manager", "auto_continue_manager")


class AutoContinueControl(ctk.CTkFrame):
    """Control widget for auto-continue functionality."""

    def __init__(self, master, provider: str, **kwargs):
        frame_kwargs = card_frame_kwargs()
        frame_kwargs.update(kwargs)
        super().__init__(master, **frame_kwargs)
        self.provider = provider
        self._refreshing = False
        self._refresh_generation = 0
        self._refresh_finish_after_id = None
        self._info_text = None
        self._info_text_host = None
        self._info_text_after_id = None
        self._info_placeholder_label = None
        self._pending_info_text = "正在后台读取自动续跑状态..."
        self._destroyed = False
        self._build_ui()
        self.after(20, self.refresh)

    def destroy(self):
        self._destroyed = True
        self._refresh_generation += 1
        if self._info_text_after_id:
            try:
                self.after_cancel(self._info_text_after_id)
            except Exception:
                pass
            self._info_text_after_id = None
        if self._refresh_finish_after_id:
            try:
                self.after_cancel(self._refresh_finish_after_id)
            except Exception:
                pass
            self._refresh_finish_after_id = None
        super().destroy()

    def _build_ui(self):
        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(8, 5))

        ctk.CTkLabel(
            header,
            text="自动续跑",
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(side="left")

        # Status indicator
        self._status_label = ctk.CTkLabel(
            header,
            text="未安装",
            text_color=COLORS["muted_soft"],
            font=font(11),
        )
        self._status_label.pack(side="left", padx=(10, 0))

        self._state_chips = {}
        state_summary = ctk.CTkFrame(self, fg_color="transparent")
        state_summary.pack(fill="x", padx=10, pady=(0, 4))
        state_row = ctk.CTkFrame(state_summary, fg_color="transparent")
        state_row.pack(fill="x", pady=(0, 3))
        state_row_extra = ctk.CTkFrame(state_summary, fg_color="transparent")
        state_row_extra.pack(fill="x")
        self._create_state_chip(state_row, "stop", "Stop续跑", width=92)
        self._create_state_chip(state_row, "git", "Git快照", width=92)
        self._create_state_chip(state_row, "recovery", "API恢复", width=92)
        self._create_state_chip(state_row_extra, "training", "训练续跑", width=92)
        if self.provider.lower() == "claude":
            self._create_state_chip(state_row_extra, "permission", "权限自动确认", width=108)

        # Controls
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=10, pady=(2, 6))

        # Enable/Pause button
        self._toggle_btn = ctk.CTkButton(
            controls,
            text="启用 Stop",
            width=92,
            command=self._toggle,
            **button_style("primary", compact=True),
        )
        self._toggle_btn.pack(side="left", padx=(0, 5))

        # Settings button
        ctk.CTkButton(
            controls,
            text="设置",
            width=60,
            command=self._show_settings,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(0, 5))

        # Error stats button
        ctk.CTkButton(
            controls,
            text="错误统计",
            width=80,
            command=self._show_error_stats,
            **button_style("accent", compact=True),
        ).pack(side="left", padx=(0, 5))

        ctk.CTkButton(
            controls,
            text="续跑日志",
            width=80,
            command=self._show_auto_continue_logs,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(0, 5))

        ctk.CTkButton(
            controls,
            text="Git历史",
            width=76,
            command=self._show_git_history,
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(0, 5))

        ctk.CTkFrame(controls, fg_color="transparent").pack(side="left", fill="x", expand=True)

        # Uninstall button
        ctk.CTkButton(
            controls,
            text="卸载 Hook",
            width=78,
            command=self._uninstall,
            **button_style("danger", compact=True),
        ).pack(side="left")

        quick = ctk.CTkFrame(self, fg_color="transparent")
        quick.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            quick,
            text="能力开关（互不依赖）",
            text_color=COLORS["muted"],
            font=font(12, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        feature_row = ctk.CTkFrame(quick, fg_color="transparent")
        feature_row.pack(fill="x")
        feature_row_extra = ctk.CTkFrame(quick, fg_color="transparent")
        feature_row_extra.pack(fill="x", pady=(2, 0))
        for row_frame in (feature_row, feature_row_extra):
            for column in range(3):
                row_frame.grid_columnconfigure(column, weight=0)

        self._auto_continue_var = ctk.BooleanVar(value=False)
        self._auto_continue_switch = ctk.CTkSwitch(
            feature_row,
            text="Stop 续跑",
            variable=self._auto_continue_var,
            command=lambda: self._toggle_feature("auto_continue"),
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        self._auto_continue_switch.grid(row=0, column=0, sticky="w", padx=(0, 14), pady=(0, 4))

        self._training_auto_continue_var = ctk.BooleanVar(value=False)
        self._training_auto_continue_switch = ctk.CTkSwitch(
            feature_row_extra,
            text="训练达标续跑",
            variable=self._training_auto_continue_var,
            command=lambda: self._toggle_feature("training_auto_continue"),
            text_color=COLORS["text"],
            progress_color=COLORS["accent"],
            button_color=COLORS["text"],
        )
        self._training_auto_continue_switch.grid(row=0, column=0, sticky="w", padx=(0, 14), pady=(0, 4))

        self._git_snapshot_var = ctk.BooleanVar(value=True)
        self._git_snapshot_switch = ctk.CTkSwitch(
            feature_row,
            text="Git 本地快照",
            variable=self._git_snapshot_var,
            command=lambda: self._toggle_feature("git_snapshot"),
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        self._git_snapshot_switch.grid(row=0, column=1, sticky="w", padx=(0, 14), pady=(0, 4))

        self._error_recovery_var = ctk.BooleanVar(value=False)
        self._error_recovery_switch = ctk.CTkSwitch(
            feature_row,
            text="API 错误恢复",
            variable=self._error_recovery_var,
            command=lambda: self._toggle_feature("error_recovery"),
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        self._error_recovery_switch.grid(row=0, column=2, sticky="w", padx=(0, 14), pady=(0, 4))

        self._permission_auto_approve_var = None
        self._permission_auto_approve_switch = None
        if self.provider.lower() == "claude":
            self._permission_auto_approve_var = ctk.BooleanVar(value=False)
            self._permission_auto_approve_switch = ctk.CTkSwitch(
                feature_row_extra,
                text="权限自动确认",
                variable=self._permission_auto_approve_var,
                command=lambda: self._toggle_feature("permission_auto_approve"),
                text_color=COLORS["text"],
                progress_color=COLORS["warning"],
                button_color=COLORS["text"],
            )
            self._permission_auto_approve_switch.grid(row=0, column=1, sticky="w", padx=(0, 14), pady=(0, 4))

        detail = ctk.CTkFrame(self, fg_color="transparent")
        detail.pack(fill="x", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            detail,
            text="Git 快照触发时机",
            text_color=COLORS["muted"],
            font=font(12, "bold"),
        ).pack(anchor="w", pady=(2, 4))

        git_row = ctk.CTkFrame(detail, fg_color="transparent")
        git_row.pack(fill="x")
        for column in range(3):
            git_row.grid_columnconfigure(column, weight=0)

        self._git_snapshot_on_start_var = ctk.BooleanVar(value=True)
        self._git_snapshot_on_start_switch = ctk.CTkSwitch(
            git_row,
            text="开局/消息/Stop",
            variable=self._git_snapshot_on_start_var,
            command=lambda: self._toggle_feature("git_snapshot_on_start"),
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        self._git_snapshot_on_start_switch.grid(row=0, column=0, sticky="w", padx=(0, 14), pady=(0, 4))

        self._git_snapshot_on_recovery_var = ctk.BooleanVar(value=True)
        self._git_snapshot_on_recovery_switch = ctk.CTkSwitch(
            git_row,
            text="API 恢复前",
            variable=self._git_snapshot_on_recovery_var,
            command=lambda: self._toggle_feature("git_snapshot_on_recovery"),
            text_color=COLORS["text"],
            progress_color=COLORS["success"],
            button_color=COLORS["text"],
        )
        self._git_snapshot_on_recovery_switch.grid(row=0, column=1, sticky="w", padx=(0, 14), pady=(0, 4))

        self._git_auto_push_var = ctk.BooleanVar(value=False)
        self._git_auto_push_switch = ctk.CTkSwitch(
            git_row,
            text="推送已有 Git remote",
            variable=self._git_auto_push_var,
            command=lambda: self._toggle_feature("git_auto_push"),
            text_color=COLORS["text"],
            progress_color=COLORS["accent"],
            button_color=COLORS["text"],
        )
        self._git_auto_push_switch.grid(row=0, column=2, sticky="w", padx=(0, 14), pady=(0, 4))

        # Info display
        self._info_text_host = ctk.CTkFrame(
            self,
            height=118,
            fg_color=COLORS["field_bg"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border"],
        )
        self._info_text_host.pack(fill="x", padx=10, pady=(5, 8))
        self._info_text_host.pack_propagate(False)
        self._info_placeholder_label = ctk.CTkLabel(
            self._info_text_host,
            text="正在后台读取自动续跑状态...",
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._info_placeholder_label.pack(expand=True)
        self._info_text_after_id = self.after(60, self._build_info_text)

    def _build_info_text(self):
        self._info_text_after_id = None
        if self._destroyed or self._info_text or not self._info_text_host:
            return
        try:
            for child in self._info_text_host.winfo_children():
                child.destroy()
        except Exception:
            pass
        self._info_text = ctk.CTkTextbox(self._info_text_host, height=118, **textbox_style(monospace=True))
        self._info_text.pack(fill="both", expand=True)
        self._set_info_text(self._pending_info_text)

    def _set_info_text(self, text: str):
        self._pending_info_text = str(text or "")
        if not self._info_text:
            if self._info_placeholder_label:
                try:
                    preview = self._pending_info_text.splitlines()[0] if self._pending_info_text else ""
                    self._info_placeholder_label.configure(text=preview[:120])
                except Exception:
                    pass
            return
        self._info_text.configure(state="normal")
        self._info_text.delete("1.0", "end")
        self._info_text.insert("1.0", self._pending_info_text)
        self._info_text.configure(state="disabled")

    def _create_state_chip(self, parent, key: str, label: str, width: int = 88):
        chip = ctk.CTkLabel(
            parent,
            text=f"{label} OFF",
            width=width,
            height=24,
            corner_radius=12,
            fg_color=COLORS["surface_alt"],
            text_color=COLORS["muted"],
            font=font(11, "bold"),
        )
        chip.pack(side="left", padx=(0, 8), pady=(0, 2))
        self._state_chips[key] = (chip, label)
        return chip

    def _set_state_chip(self, key: str, enabled: bool):
        item = self._state_chips.get(key)
        if not item:
            return
        chip, label = item
        chip.configure(
            text=f"{label} {'ON' if enabled else 'OFF'}",
            text_color=COLORS["success"] if enabled else COLORS["muted"],
            fg_color=COLORS["surface_alt"],
        )

    def refresh(self):
        """Refresh status display."""
        self._refresh_generation += 1
        generation = self._refresh_generation
        try:
            self._status_label.configure(text="读取中...", text_color=COLORS["muted_soft"])
            self._set_info_text("正在后台读取自动续跑状态...")
        except Exception:
            pass

        def worker():
            try:
                payload = {
                    "ok": True,
                    "status": auto_continue_manager.get_status(self.provider),
                    "settings": auto_continue_manager.get_settings(self.provider),
                    "error": "",
                }
            except Exception as e:
                payload = {"ok": False, "status": None, "settings": None, "error": str(e)}

            def finish():
                self._refresh_finish_after_id = None
                if generation != self._refresh_generation or not self._is_alive():
                    return
                if not payload["ok"]:
                    self._apply_refresh_error(payload["error"])
                    return
                self._apply_refresh_payload(payload["status"], payload["settings"])

            try:
                if not self._destroyed:
                    self._refresh_finish_after_id = self.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, name=f"auto-continue-refresh-{self.provider}", daemon=True).start()

    def _is_alive(self) -> bool:
        if self._destroyed:
            return False
        try:
            return bool(self.winfo_exists())
        except Exception:
            return False

    def _apply_refresh_error(self, message: str):
        try:
            self._status_label.configure(text="错误", text_color="#e74c3c")
            self._set_info_text(f"错误: {message}")
        except Exception:
            pass

    def _apply_refresh_payload(self, status, settings):
        try:
            display_settings = settings or AutoContinueSettings()

            # Update status label. This describes the hook installation state;
            # the Stop auto-continue state is shown separately to avoid implying
            # that Git/API recovery are paused when only Stop continuation is off.
            if status.hook_registered:
                self._status_label.configure(text="Hook 运行中", text_color=COLORS["success"])
            elif status.hook_script_exists:
                self._status_label.configure(text="脚本已安装", text_color=COLORS["warning"])
            elif status.error_recovery_installed:
                self._status_label.configure(text="错误恢复已安装", text_color=COLORS["warning"])
            else:
                self._status_label.configure(text="未安装 Hook", text_color=COLORS["muted_soft"])

            if display_settings.enabled:
                self._toggle_btn.configure(text="暂停 Stop", **button_style("warning", compact=True))
            else:
                self._toggle_btn.configure(text="启用 Stop", **button_style("primary", compact=True))

            self._set_state_chip("stop", bool(display_settings.enabled))
            self._set_state_chip("training", bool(display_settings.training_auto_continue_enabled))
            self._set_state_chip("git", bool(display_settings.git_auto_snapshot))
            self._set_state_chip("recovery", bool(display_settings.error_recovery_enabled))
            if self.provider.lower() == "claude":
                self._set_state_chip(
                    "permission",
                    bool(display_settings.auto_approve_permission_requests),
                )

            self._refreshing = True
            try:
                self._auto_continue_var.set(bool(display_settings.enabled))
                self._training_auto_continue_var.set(bool(display_settings.training_auto_continue_enabled))
                self._git_snapshot_var.set(bool(display_settings.git_auto_snapshot))
                self._git_auto_push_var.set(bool(display_settings.git_auto_push))
                self._git_snapshot_on_start_var.set(bool(display_settings.git_snapshot_on_start))
                self._git_snapshot_on_recovery_var.set(bool(display_settings.git_snapshot_on_recovery))
                self._error_recovery_var.set(bool(display_settings.error_recovery_enabled))
                if self._permission_auto_approve_var is not None:
                    self._permission_auto_approve_var.set(
                        bool(display_settings.auto_approve_permission_requests)
                    )
                for switch in [
                    self._auto_continue_switch,
                    self._training_auto_continue_switch,
                    self._git_snapshot_switch,
                    self._git_auto_push_switch,
                    self._error_recovery_switch,
                    self._permission_auto_approve_switch,
                ]:
                    if switch is not None:
                        switch.configure(state="normal")
                git_trigger_state = "normal" if display_settings.git_auto_snapshot else "disabled"
                self._git_snapshot_on_start_switch.configure(state=git_trigger_state)
                self._git_snapshot_on_recovery_switch.configure(state=git_trigger_state)
                self._git_auto_push_switch.configure(state=git_trigger_state)
            finally:
                self._refreshing = False

            # Update info text
            info_lines = []
            info_lines.append(f"Hook 脚本: {'✓' if status.hook_script_exists else '✗'}")
            info_lines.append(f"Hook 已注册: {'✓' if status.hook_registered else '✗'}")
            if self.provider.lower() == "claude":
                info_lines.append(f"Guidance: {'✓' if status.guidance_installed else '✗'}")
            info_lines.append(f"错误恢复 Hook: {'✓ 已安装' if status.error_recovery_installed else '✗ 未安装'}")

            if settings:
                info_lines.append(
                    f"Stop续跑: {'ON' if settings.enabled else 'OFF'} / "
                    f"最大 {settings.max_continuations} / 保守 {'ON' if settings.conservative_mode else 'OFF'}"
                )
                info_lines.append(
                    f"训练续跑: {'ON' if settings.training_auto_continue_enabled else 'OFF'}"
                )
                if settings.training_auto_continue_enabled:
                    template = training_prompt_template_by_key(settings.training_prompt_template_key)
                    info_lines.append(f"训练模板: {template['name']}")
                info_lines.append(
                    f"Git快照: {'ON' if settings.git_auto_snapshot else 'OFF'} / "
                    f"推送已有 Git remote {'ON' if settings.git_auto_push else 'OFF'} / "
                    f"对话/消息/Stop {'ON' if settings.git_snapshot_on_start else 'OFF'} / "
                    f"API恢复 {'ON' if settings.git_snapshot_on_recovery else 'OFF'}"
                )
                if settings.error_recovery_enabled:
                    info_lines.append(f"最大恢复次数: {settings.max_error_recoveries}")
                    info_lines.append(
                        "断联重试间隔: "
                        f"{settings.error_retry_initial_delay_seconds}-"
                        f"{settings.error_retry_max_delay_seconds}s"
                    )
                if self.provider.lower() == "claude":
                    info_lines.append(f"应用到 Subagent: {'是' if settings.apply_to_subagents else '否'}")
                    auto_approve_limit = (
                        "一直"
                        if settings.auto_approve_max_per_session == 0
                        else str(settings.auto_approve_max_per_session)
                    )
                    auto_approve_tools = list(settings.auto_approve_tools[:5])
                    info_lines.append(
                        f"权限自动确认: {'ON' if settings.auto_approve_permission_requests else 'OFF'}"
                        f" / {auto_approve_limit} 次 / {', '.join(auto_approve_tools[:5])}"
                    )

            self._set_info_text("\n".join(info_lines))

        except Exception as e:
            self._apply_refresh_error(str(e))

    def _save_settings(self, settings: AutoContinueSettings) -> None:
        auto_continue_manager.update_settings(self.provider, settings)

    def _toggle_feature(self, feature: str):
        if self._refreshing:
            return
        try:
            settings = auto_continue_manager.get_settings(self.provider) or AutoContinueSettings()
            if feature == "auto_continue":
                if bool(self._auto_continue_var.get()):
                    apply_to_subagents = (
                        settings.apply_to_subagents
                        if self.provider.lower() == "claude"
                        else False
                    )
                    auto_continue_manager.enable(self.provider, settings, apply_to_subagents)
                else:
                    auto_continue_manager.pause(self.provider)
                show_toast(self.winfo_toplevel(), "Stop 续跑开关已更新")
                self.refresh()
                return
            elif feature == "git_snapshot":
                settings.git_auto_snapshot = self._git_snapshot_var.get()
            elif feature == "git_auto_push":
                settings.git_auto_push = bool(self._git_auto_push_var.get())
            elif feature == "training_auto_continue":
                settings.training_auto_continue_enabled = bool(self._training_auto_continue_var.get())
            elif feature == "git_snapshot_on_start":
                settings.git_snapshot_on_start = bool(self._git_snapshot_on_start_var.get())
            elif feature == "git_snapshot_on_recovery":
                settings.git_snapshot_on_recovery = bool(self._git_snapshot_on_recovery_var.get())
            elif feature == "error_recovery":
                settings.error_recovery_enabled = self._error_recovery_var.get()
            elif feature == "permission_auto_approve":
                settings.auto_approve_permission_requests = bool(
                    self._permission_auto_approve_var and self._permission_auto_approve_var.get()
                )
            else:
                return

            self._save_settings(settings)
            show_toast(self.winfo_toplevel(), "独立开关已更新")
            self.refresh()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"开关更新失败: {e}", is_error=True)
            self.refresh()

    def _toggle(self):
        """Toggle enable/pause."""
        try:
            status = auto_continue_manager.get_status(self.provider)

            if status.enabled:
                # Pause
                auto_continue_manager.pause(self.provider)
                show_toast(self.winfo_toplevel(), f"{self.provider} Stop 续跑已暂停")
            else:
                # Enable
                settings = auto_continue_manager.get_settings(self.provider)
                if settings is None:
                    settings = AutoContinueSettings()

                apply_to_subagents = settings.apply_to_subagents if self.provider.lower() == "claude" else False
                auto_continue_manager.enable(self.provider, settings, apply_to_subagents)
                show_toast(self.winfo_toplevel(), f"{self.provider} Stop 续跑已启用")

            self.refresh()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"操作失败: {e}", is_error=True)

    def _show_settings(self):
        """Show settings dialog."""
        from ui.dialogs.auto_continue_settings import AutoContinueSettingsDialog
        settings = auto_continue_manager.get_settings(self.provider) or AutoContinueSettings()

        def on_save(new_settings):
            self._save_settings(new_settings)
            show_toast(self.winfo_toplevel(), "设置已保存")
            self.refresh()

        AutoContinueSettingsDialog(self.winfo_toplevel(), self.provider, settings, on_save)

    def _uninstall(self):
        """Uninstall auto-continue."""
        from ui.dialogs.confirm_dialog import ConfirmDialog

        def do_uninstall():
            try:
                auto_continue_manager.uninstall(self.provider)
                show_toast(self.winfo_toplevel(), f"{self.provider} Hook 已卸载")
                self.refresh()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"卸载失败: {e}", is_error=True)

        ConfirmDialog(self.winfo_toplevel(), title="确认卸载 Hook",
                      message=f"确定要卸载 {self.provider} 的 Hook 功能吗？\n这将删除 Stop 续跑、Git 快照和错误恢复相关脚本与配置。",
                      on_confirm=do_uninstall)

    def _show_error_stats(self):
        """显示错误统计"""
        from ui.dialogs.error_stats_dialog import ErrorStatsDialog
        ErrorStatsDialog(self.winfo_toplevel(), self.provider)

    def _show_auto_continue_logs(self):
        """显示自动续跑日志"""
        from ui.dialogs.auto_continue_logs_dialog import AutoContinueLogsDialog
        AutoContinueLogsDialog(self.winfo_toplevel(), self.provider)

    def _show_git_history(self):
        """显示 Git 快照历史"""
        from ui.dialogs.git_snapshot_history_dialog import GitSnapshotHistoryDialog
        GitSnapshotHistoryDialog(self.winfo_toplevel())
