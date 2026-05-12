import customtkinter as ctk
import logging
from ui.tabs.claude_tab import ClaudeTab
from ui.tabs.codex_tab import CodexTab
from ui.tabs.env_tab import EnvTab
from ui.tabs.common_tab import CommonTab
from ui.tabs.backup_tab import BackupTab
from ui.tabs.ssh_tab import SSHTab
from ui.tabs.browser_tab import BrowserTab
from ui.tabs.session_migration_tab import SessionMigrationTab
from ui.tabs.log_viewer_tab import LogViewerTab
from ui.tabs.usage_stats_tab import UsageStatsTab
from ui.theme import COLORS, bind_wraplength, button_style, combo_style, font
from core.tray_manager import TrayManager
from core import profile_manager

logger = logging.getLogger(__name__)
ENV_TAB_LABEL = "环境变量"


class App(ctk.CTk):
    """Main application window."""

    def __init__(self, start_minimized: bool = False):
        super().__init__()

        self.title("API 配置切换器")
        self.geometry("1120x760")
        self.minsize(980, 620)
        self.configure(fg_color=COLORS["app_bg"])
        self._exit_requested = False
        self._tray_hint_shown = False
        self._close_dialog = None

        # Initialize tray manager
        self.tray_manager = TrayManager(
            on_show_window=self._show_window,
            on_exit=self._exit_app,
            on_startup_changed=self._on_startup_changed_from_tray,
            on_hide_window=self._hide_to_tray,
        )
        self._start_minimized_to_tray = bool(start_minimized and self.tray_manager.is_available())
        if self._start_minimized_to_tray:
            self.withdraw()

        # Handle window close event (minimize to tray instead of exit)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=20, pady=(18, 14))

        # Top bar
        topbar = ctk.CTkFrame(shell, fg_color="transparent")
        topbar.pack(fill="x", pady=(0, 14))

        title_area = ctk.CTkFrame(topbar, fg_color="transparent")
        title_area.pack(fill="x")

        ctk.CTkLabel(
            title_area,
            text="API 配置切换器",
            text_color=COLORS["text"],
            font=font(22, "bold"),
        ).pack(anchor="w")

        subtitle_label = ctk.CTkLabel(
            title_area,
            text="第三方 API 配置、官方账号快照、本机浏览器 Profile 分区管理",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        subtitle_label.pack(anchor="w", fill="x", pady=(2, 0))
        bind_wraplength(title_area, subtitle_label, padding=12, min_width=260, max_width=620)

        action_panel = ctk.CTkFrame(
            topbar,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        action_panel.pack(fill="x", pady=(10, 0))

        # Quick switch menu
        switch_frame = ctk.CTkFrame(action_panel, fg_color="transparent")
        switch_frame.pack(side="left", padx=(12, 8), pady=9)

        ctk.CTkLabel(
            switch_frame,
            text="快速切换 API",
            text_color=COLORS["muted"],
            font=font(11),
        ).pack(side="left", padx=(0, 8))

        # Claude quick switch
        self.claude_switch = ctk.CTkComboBox(
            switch_frame,
            width=148,
            command=self._quick_switch_claude,
            **combo_style(),
        )
        self.claude_switch.pack(side="left", padx=(0, 6))
        self.claude_switch.set("Claude API")

        # Codex quick switch
        self.codex_switch = ctk.CTkComboBox(
            switch_frame,
            width=148,
            command=self._quick_switch_codex,
            **combo_style(),
        )
        self.codex_switch.pack(side="left")
        self.codex_switch.set("Codex API")

        # 按钮区域
        button_group = ctk.CTkFrame(action_panel, fg_color="transparent")
        button_group.pack(side="right", padx=(0, 12), pady=9)
        ctk.CTkButton(
            button_group,
            text=ENV_TAB_LABEL,
            width=96,
            command=self._show_env_tab,
            **button_style("primary"),
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            button_group,
            text="健康检查",
            width=96,
            command=self._show_health_check,
            **button_style("accent"),
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            button_group,
            text="回滚上次",
            width=96,
            command=self._restore_latest_backup,
            **button_style("warning"),
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            button_group,
            text="刷新全部",
            width=96,
            command=self.refresh_all,
            **button_style("secondary"),
        ).pack(side="left", padx=(8, 0))

        # Tab view
        self._tabview = ctk.CTkTabview(
            shell,
            corner_radius=8,
            border_width=1,
            fg_color=COLORS["surface"],
            border_color=COLORS["border_soft"],
            segmented_button_fg_color=COLORS["app_bg"],
            segmented_button_selected_color=COLORS["primary"],
            segmented_button_selected_hover_color=COLORS["primary_hover"],
            segmented_button_unselected_color=COLORS["surface_alt"],
            segmented_button_unselected_hover_color=COLORS["surface_hover"],
            text_color=COLORS["text"],
        )
        self._tabview.pack(fill="both", expand=True)

        # Create tabs
        self._claude_tab = ClaudeTab(self._tabview.add("Claude Code"))
        self._codex_tab = CodexTab(self._tabview.add("Codex CLI"))
        self._env_tab = EnvTab(self._tabview.add(ENV_TAB_LABEL))
        self._browser_tab = BrowserTab(self._tabview.add("浏览器 Profile"))
        self._session_migration_tab = SessionMigrationTab(self._tabview.add("会话迁移"))
        self._ssh_tab = SSHTab(self._tabview.add("SSH 服务器"))
        self._common_tab = CommonTab(self._tabview.add("通用设置"))
        self._usage_stats_tab = UsageStatsTab(self._tabview.add("使用统计"))
        self._backup_tab = BackupTab(self._tabview.add("备份管理"))
        self._log_viewer_tab = LogViewerTab(self._tabview.add("日志查看器"))

        # Make tabs fill the space
        for tab in [self._claude_tab, self._codex_tab, self._env_tab, self._browser_tab,
                    self._session_migration_tab, self._ssh_tab, self._common_tab, self._usage_stats_tab,
                    self._backup_tab, self._log_viewer_tab]:
            tab.pack(fill="both", expand=True)

        # Status bar
        footer = ctk.CTkFrame(
            shell,
            fg_color=COLORS["surface"],
            corner_radius=6,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        footer.pack(fill="x", pady=(8, 0))
        self._status = ctk.CTkLabel(
            footer,
            text="就绪",
            text_color=COLORS["muted"],
            font=font(11),
        )
        self._status.pack(anchor="w", padx=10, pady=6)

        # Start tray icon when optional dependency is available.
        if self.tray_manager.is_available():
            self.tray_manager.start()
            logger.info("Tray icon started")
        else:
            logger.info("Tray icon disabled: pystray is not installed")

        # Load quick switch profiles
        self._load_quick_switch_profiles()

    def _load_quick_switch_profiles(self):
        """Load profiles for quick switch menus."""
        try:
            # Load Claude profiles
            claude_profiles = profile_manager.list_switchable_claude_profiles()
            claude_names = [p.name for p in claude_profiles]
            if claude_names:
                self.claude_switch.configure(values=claude_names, state="normal")
                # Set current active profile
                current = profile_manager.get_current_claude_name() or profile_manager.get_active_claude_name()
                if current in claude_names:
                    self.claude_switch.set(current)
                else:
                    self.claude_switch.set(claude_names[0] if claude_names else "Claude API")
            else:
                self.claude_switch.configure(values=["暂无 Claude API 配置"], state="disabled")
                self.claude_switch.set("暂无 Claude API 配置")

            # Load Codex profiles
            codex_profiles = profile_manager.list_switchable_codex_profiles()
            codex_names = [p.name for p in codex_profiles]
            if codex_names:
                self.codex_switch.configure(values=codex_names, state="normal")
                # Set current active profile
                current = profile_manager.get_current_codex_name() or profile_manager.get_active_codex_name()
                if current in codex_names:
                    self.codex_switch.set(current)
                else:
                    self.codex_switch.set(codex_names[0] if codex_names else "Codex API")
            else:
                self.codex_switch.configure(values=["暂无 Codex API 配置"], state="disabled")
                self.codex_switch.set("暂无 Codex API 配置")

        except Exception as e:
            logger.error(f"Failed to load quick switch profiles: {e}", exc_info=True)

    def _quick_switch_claude(self, profile_name: str):
        """Quick switch Claude profile."""
        if profile_name in {"Claude", "Claude Profile", "Claude API", "暂无 Claude Profile", "暂无 Claude API 配置", "无配置"}:
            return

        def perform_switch():
            try:
                from core import switcher
                from ui.widgets.toast import show_toast

                switcher.switch_claude_profile(profile_name)

                show_toast(self, f"已切换 Claude API 配置: {profile_name}")
                self._status.configure(text=f"已切换 Claude API 配置: {profile_name}")

                self._claude_tab.refresh()
                self._usage_stats_tab.refresh()
                self._load_quick_switch_profiles()
                if self.tray_manager.is_running():
                    self.tray_manager.update_menu()

                logger.info(f"Quick switched to Claude profile: {profile_name}")

            except Exception as e:
                logger.error(f"Failed to quick switch Claude: {e}", exc_info=True)
                from ui.widgets.toast import show_toast
                show_toast(self, f"切换失败: {e}", is_error=True)
                self._load_quick_switch_profiles()

        self._show_switch_preview("claude_api", profile_name, perform_switch, self._load_quick_switch_profiles)

    def _quick_switch_codex(self, profile_name: str):
        """Quick switch Codex profile."""
        if profile_name in {"Codex", "Codex Profile", "Codex API", "暂无 Codex Profile", "暂无 Codex API 配置", "无配置"}:
            return

        def perform_switch():
            try:
                from core import switcher
                from ui.widgets.toast import show_toast

                switcher.switch_codex_profile(profile_name)

                show_toast(self, f"已切换 Codex API 配置: {profile_name}")
                self._status.configure(text=f"已切换 Codex API 配置: {profile_name}")

                self._codex_tab.refresh()
                self._usage_stats_tab.refresh()
                self._load_quick_switch_profiles()
                if self.tray_manager.is_running():
                    self.tray_manager.update_menu()

                logger.info(f"Quick switched to Codex profile: {profile_name}")

            except Exception as e:
                logger.error(f"Failed to quick switch Codex: {e}", exc_info=True)
                from ui.widgets.toast import show_toast
                show_toast(self, f"切换失败: {e}", is_error=True)
                self._load_quick_switch_profiles()

        self._show_switch_preview("codex_api", profile_name, perform_switch, self._load_quick_switch_profiles)

    def _show_switch_preview(self, kind: str, profile_name: str, on_confirm, on_cancel=None):
        try:
            from ui.dialogs.switch_preview_dialog import show_switch_preview

            show_switch_preview(self, kind, profile_name, on_confirm=on_confirm, on_cancel=on_cancel)
        except Exception as e:
            logger.error(f"Failed to show switch preview: {e}", exc_info=True)
            from ui.widgets.toast import show_toast
            show_toast(self, f"切换预览失败: {e}", is_error=True)
            if on_cancel:
                on_cancel()

    def _on_closing(self):
        """Ask whether the close button should exit or minimize to tray."""
        if self._exit_requested:
            return
        if not self.tray_manager.is_available() or not self.tray_manager.is_running():
            self._exit_app()
            return

        if self._close_dialog and self._close_dialog.winfo_exists():
            self._close_dialog.lift()
            self._close_dialog.focus_force()
            return

        try:
            from ui.dialogs.close_choice_dialog import CloseChoiceDialog

            self._close_dialog = CloseChoiceDialog(
                self,
                on_minimize=self._close_dialog_minimize,
                on_exit=self._close_dialog_exit,
                on_cancel=self._clear_close_dialog,
            )
        except Exception as e:
            logger.error("Failed to show close choice dialog: %s", e, exc_info=True)
            self._hide_to_tray()

    def _clear_close_dialog(self):
        self._close_dialog = None

    def _close_dialog_minimize(self):
        self._clear_close_dialog()
        self._hide_to_tray()

    def _close_dialog_exit(self):
        self._clear_close_dialog()
        self._exit_app()

    def _hide_to_tray(self, icon=None, item=None):
        """Hide the main window to the system tray."""
        self._run_on_ui_thread(self._hide_to_tray_now)

    def _hide_to_tray_now(self):
        if not self.tray_manager.is_available():
            return
        self.withdraw()
        if self.tray_manager.is_running() and not self._tray_hint_shown and not self._start_minimized_to_tray:
            self.tray_manager.notify("程序已在后台运行，右键托盘图标可恢复或退出。")
            self._tray_hint_shown = True
        logger.info("Window minimized to tray")

    def _show_window(self, icon=None, item=None):
        """Show the main window from tray."""
        self._run_on_ui_thread(self._show_window_now)

    def _show_window_now(self):
        self._start_minimized_to_tray = False
        self.deiconify()
        try:
            self.state("normal")
        except Exception:
            pass
        self.lift()
        self.focus_force()
        logger.info("Window restored from tray")

    def _exit_app(self):
        """Exit the application completely."""
        self._run_on_ui_thread(self._exit_app_now)

    def _exit_app_now(self):
        if self._exit_requested:
            return
        self._exit_requested = True
        logger.info("Exiting application")
        self.tray_manager.stop()
        self.quit()
        self.destroy()

    def _on_startup_changed_from_tray(self):
        def refresh_startup():
            if hasattr(self, "_common_tab"):
                self._common_tab.refresh()

        self._run_on_ui_thread(refresh_startup)

    def _run_on_ui_thread(self, callback):
        try:
            if self.winfo_exists():
                self.after(0, callback)
        except Exception as e:
            logger.debug("Failed to schedule UI callback: %s", e)

    def _show_env_tab(self):
        self._tabview.set(ENV_TAB_LABEL)
        self._env_tab.refresh()
        self._status.configure(text="已打开环境变量管理")

    def refresh_all(self):
        """Refresh all tabs."""
        self._claude_tab.refresh()
        self._codex_tab.refresh()
        self._env_tab.refresh()
        self._browser_tab.refresh()
        self._session_migration_tab.refresh()
        self._ssh_tab.refresh()
        self._common_tab.refresh()
        self._usage_stats_tab.refresh()
        self._backup_tab.refresh()
        self._log_viewer_tab.refresh()
        self._status.configure(text="已刷新全部 API 配置和账号状态")

        # Update tray menu to reflect changes
        if self.tray_manager.is_running():
            self.tray_manager.update_menu()

        # Reload quick switch profiles
        self._load_quick_switch_profiles()

    def _restore_latest_backup(self):
        """Restore the newest config backup after confirmation."""
        from core import backup_manager
        from ui.dialogs.confirm_dialog import ConfirmDialog
        from ui.widgets.toast import show_toast

        entry = backup_manager.get_latest_backup()
        if not entry:
            show_toast(self, "暂无可回滚的备份", is_error=True)
            return

        def do_restore():
            try:
                restored = backup_manager.restore_backup(entry)
                self.refresh_all()
                show_toast(self, f"已回滚 {len(restored)} 个配置文件")
                self._status.configure(text=f"已回滚到备份: {entry.timestamp}")
            except Exception as e:
                logger.error(f"Failed to restore latest backup: {e}", exc_info=True)
                show_toast(self, f"回滚失败: {e}", is_error=True)

        ConfirmDialog(
            self,
            title="回滚上一次配置",
            message=(
                f"确定要回滚到最近备份吗？\n\n"
                f"时间: {entry.timestamp}\n"
                f"说明: {entry.description or '-'}\n\n"
                "当前配置会先自动备份，然后再执行回滚。"
            ),
            on_confirm=do_restore,
        )

    def _show_health_check(self):
        """显示健康检查对话框"""
        from ui.dialogs.health_check_dialog import HealthCheckDialog
        dialog = HealthCheckDialog(self)
        dialog.focus()
