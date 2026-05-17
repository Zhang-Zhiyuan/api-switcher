import customtkinter as ctk
from models.auto_continue import AutoContinueSettings
from core.auto_continue.manager import auto_continue_manager
from ui.widgets.toast import show_toast
from ui.theme import COLORS, button_style, card_frame_kwargs, font, textbox_style


class AutoContinueControl(ctk.CTkFrame):
    """Control widget for auto-continue functionality."""

    def __init__(self, master, provider: str, **kwargs):
        frame_kwargs = card_frame_kwargs()
        frame_kwargs.update(kwargs)
        super().__init__(master, **frame_kwargs)
        self.provider = provider
        self._build_ui()
        self.refresh()

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

        # Controls
        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=10, pady=5)

        # Enable/Pause button
        self._toggle_btn = ctk.CTkButton(
            controls,
            text="启用",
            width=80,
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

        # Uninstall button
        ctk.CTkButton(
            controls,
            text="卸载",
            width=60,
            command=self._uninstall,
            **button_style("danger", compact=True),
        ).pack(side="left")

        # Info display
        self._info_text = ctk.CTkTextbox(self, height=82, **textbox_style(monospace=True))
        self._info_text.pack(fill="x", padx=10, pady=(5, 8))

    def refresh(self):
        """Refresh status display."""
        try:
            status = auto_continue_manager.get_status(self.provider)
            settings = auto_continue_manager.get_settings(self.provider)

            # Update status label
            if status.enabled:
                self._status_label.configure(text="已启用", text_color=COLORS["success"])
                self._toggle_btn.configure(text="暂停", **button_style("warning", compact=True))
            elif status.hook_script_exists or status.hook_registered:
                self._status_label.configure(text="已暂停", text_color=COLORS["warning"])
                self._toggle_btn.configure(text="启用", **button_style("primary", compact=True))
            else:
                self._status_label.configure(text="未安装", text_color=COLORS["muted_soft"])
                self._toggle_btn.configure(text="启用", **button_style("primary", compact=True))

            # Update info text
            info_lines = []
            info_lines.append(f"Hook 脚本: {'✓' if status.hook_script_exists else '✗'}")
            info_lines.append(f"Hook 已注册: {'✓' if status.hook_registered else '✗'}")
            if self.provider.lower() == "claude":
                info_lines.append(f"Guidance: {'✓' if status.guidance_installed else '✗'}")
            info_lines.append(f"错误恢复: {'✓ 已启用' if status.error_recovery_installed else '✗ 未启用'}")

            if settings:
                info_lines.append(f"Git snapshot: {'ON' if settings.git_auto_snapshot and settings.git_snapshot_on_start else 'OFF'}")
                info_lines.append(f"最大续跑次数: {settings.max_continuations}")
                info_lines.append(f"保守模式: {'是' if settings.conservative_mode else '否'}")
                if settings.error_recovery_enabled:
                    info_lines.append(f"最大恢复次数: {settings.max_error_recoveries}")
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

            self._info_text.configure(state="normal")
            self._info_text.delete("1.0", "end")
            self._info_text.insert("1.0", "\n".join(info_lines))
            self._info_text.configure(state="disabled")

        except Exception as e:
            self._status_label.configure(text="错误", text_color="#e74c3c")
            self._info_text.configure(state="normal")
            self._info_text.delete("1.0", "end")
            self._info_text.insert("1.0", f"错误: {e}")
            self._info_text.configure(state="disabled")

    def _toggle(self):
        """Toggle enable/pause."""
        try:
            status = auto_continue_manager.get_status(self.provider)

            if status.enabled:
                # Pause
                auto_continue_manager.pause(self.provider)
                show_toast(self.winfo_toplevel(), f"{self.provider} 自动续跑已暂停")
            else:
                # Enable
                settings = auto_continue_manager.get_settings(self.provider)
                if settings is None:
                    settings = AutoContinueSettings()

                apply_to_subagents = settings.apply_to_subagents if self.provider.lower() == "claude" else False
                auto_continue_manager.enable(self.provider, settings, apply_to_subagents)
                show_toast(self.winfo_toplevel(), f"{self.provider} 自动续跑已启用")

            self.refresh()
        except Exception as e:
            show_toast(self.winfo_toplevel(), f"操作失败: {e}", is_error=True)

    def _show_settings(self):
        """Show settings dialog."""
        from ui.dialogs.auto_continue_settings import AutoContinueSettingsDialog
        settings = auto_continue_manager.get_settings(self.provider) or AutoContinueSettings()

        def on_save(new_settings):
            auto_continue_manager.update_settings(self.provider, new_settings)

            # 处理错误恢复功能的启用/禁用
            if new_settings.error_recovery_enabled:
                auto_continue_manager.enable_error_recovery(self.provider)
            else:
                auto_continue_manager.disable_error_recovery(self.provider)

            show_toast(self.winfo_toplevel(), "设置已保存")
            self.refresh()

        AutoContinueSettingsDialog(self.winfo_toplevel(), self.provider, settings, on_save)

    def _uninstall(self):
        """Uninstall auto-continue."""
        from ui.dialogs.confirm_dialog import ConfirmDialog

        def do_uninstall():
            try:
                auto_continue_manager.uninstall(self.provider)
                show_toast(self.winfo_toplevel(), f"{self.provider} 自动续跑已卸载")
                self.refresh()
            except Exception as e:
                show_toast(self.winfo_toplevel(), f"卸载失败: {e}", is_error=True)

        ConfirmDialog(self.winfo_toplevel(), title="确认卸载",
                      message=f"确定要卸载 {self.provider} 的自动续跑功能吗？\n这将删除 hook 脚本和配置文件。",
                      on_confirm=do_uninstall)

    def _show_error_stats(self):
        """显示错误统计"""
        from ui.dialogs.error_stats_dialog import ErrorStatsDialog
        ErrorStatsDialog(self.winfo_toplevel(), self.provider)
