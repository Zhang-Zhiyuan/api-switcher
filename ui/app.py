import customtkinter as ctk
import logging
from ui.tabs.claude_tab import ClaudeTab
from ui.tabs.codex_tab import CodexTab
from ui.tabs.common_tab import CommonTab
from ui.tabs.backup_tab import BackupTab
from ui.tabs.ssh_tab import SSHTab
from ui.tabs.browser_tab import BrowserTab
from ui.tabs.log_viewer_tab import LogViewerTab
from ui.tabs.usage_stats_tab import UsageStatsTab
from ui.theme import COLORS, button_style, font
from core.tray_manager import TrayManager
from core import profile_manager

logger = logging.getLogger(__name__)


class App(ctk.CTk):
    """Main application window."""

    def __init__(self):
        super().__init__()

        self.title("API 配置切换器")
        self.geometry("1040x720")
        self.minsize(860, 560)
        self.configure(fg_color=COLORS["app_bg"])

        # Initialize tray manager
        self.tray_manager = TrayManager(
            on_show_window=self._show_window,
            on_exit=self._exit_app
        )

        # Handle window close event (minimize to tray instead of exit)
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

        shell = ctk.CTkFrame(self, fg_color="transparent")
        shell.pack(fill="both", expand=True, padx=18, pady=(16, 12))

        # Top bar
        topbar = ctk.CTkFrame(shell, fg_color="transparent")
        topbar.pack(fill="x", pady=(0, 12))

        title_area = ctk.CTkFrame(topbar, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            title_area,
            text="API 配置切换器",
            text_color=COLORS["text"],
            font=font(24, "bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            title_area,
            text="集中管理 Claude Code、Codex CLI、SSH 同步与备份",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        # Quick switch menu
        switch_frame = ctk.CTkFrame(topbar, fg_color="transparent")
        switch_frame.pack(side="right", padx=(0, 10))

        ctk.CTkLabel(
            switch_frame,
            text="快速切换:",
            text_color=COLORS["muted"],
            font=font(11),
        ).pack(side="left", padx=(0, 5))

        # Claude quick switch
        self.claude_switch = ctk.CTkComboBox(
            switch_frame,
            width=150,
            command=self._quick_switch_claude,
            fg_color=COLORS["surface"],
            button_color=COLORS["primary"],
            button_hover_color=COLORS["primary_hover"],
            border_color=COLORS["border_soft"],
        )
        self.claude_switch.pack(side="left", padx=2)
        self.claude_switch.set("Claude")

        # Codex quick switch
        self.codex_switch = ctk.CTkComboBox(
            switch_frame,
            width=150,
            command=self._quick_switch_codex,
            fg_color=COLORS["surface"],
            button_color=COLORS["primary"],
            button_hover_color=COLORS["primary_hover"],
            border_color=COLORS["border_soft"],
        )
        self.codex_switch.pack(side="left", padx=2)
        self.codex_switch.set("Codex")

        # 按钮区域
        ctk.CTkButton(
            topbar,
            text="健康检查",
            width=96,
            command=self._show_health_check,
            **button_style("accent"),
        ).pack(side="right", padx=(10, 0))

        ctk.CTkButton(
            topbar,
            text="刷新全部",
            width=96,
            command=self.refresh_all,
            **button_style("secondary"),
        ).pack(side="right", padx=(10, 0))

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
        self._browser_tab = BrowserTab(self._tabview.add("浏览器 Profile"))
        self._ssh_tab = SSHTab(self._tabview.add("SSH 服务器"))
        self._common_tab = CommonTab(self._tabview.add("通用设置"))
        self._usage_stats_tab = UsageStatsTab(self._tabview.add("使用统计"))
        self._backup_tab = BackupTab(self._tabview.add("备份管理"))
        self._log_viewer_tab = LogViewerTab(self._tabview.add("日志查看器"))

        # Make tabs fill the space
        for tab in [self._claude_tab, self._codex_tab, self._browser_tab, self._ssh_tab,
                    self._common_tab, self._usage_stats_tab, self._backup_tab, self._log_viewer_tab]:
            tab.pack(fill="both", expand=True)

        # Status bar
        footer = ctk.CTkFrame(shell, fg_color="transparent")
        footer.pack(fill="x", pady=(8, 0))
        self._status = ctk.CTkLabel(
            footer,
            text="就绪",
            text_color=COLORS["muted"],
            font=font(11),
        )
        self._status.pack(anchor="w")

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
            claude_profiles = profile_manager.list_claude_profiles()
            claude_names = [p.name for p in claude_profiles]
            if claude_names:
                self.claude_switch.configure(values=claude_names)
                # Set current active profile
                current = profile_manager.get_active_claude_name()
                if current:
                    self.claude_switch.set(current)
                else:
                    self.claude_switch.set(claude_names[0] if claude_names else "Claude")
            else:
                self.claude_switch.configure(values=["无配置"])
                self.claude_switch.set("无配置")

            # Load Codex profiles
            codex_profiles = profile_manager.list_codex_profiles()
            codex_names = [p.name for p in codex_profiles]
            if codex_names:
                self.codex_switch.configure(values=codex_names)
                # Set current active profile
                current = profile_manager.get_active_codex_name()
                if current:
                    self.codex_switch.set(current)
                else:
                    self.codex_switch.set(codex_names[0] if codex_names else "Codex")
            else:
                self.codex_switch.configure(values=["无配置"])
                self.codex_switch.set("无配置")

        except Exception as e:
            logger.error(f"Failed to load quick switch profiles: {e}", exc_info=True)

    def _quick_switch_claude(self, profile_name: str):
        """Quick switch Claude profile."""
        if profile_name == "Claude" or profile_name == "无配置":
            return

        try:
            from core import switcher
            from core.usage_recorder import usage_recorder
            from ui.widgets.toast import show_toast

            switcher.switch_claude_profile(profile_name)
            usage_recorder.start_session(profile_name, "claude")

            show_toast(self, f"已切换到: {profile_name}")
            self._status.configure(text=f"已切换到 Claude: {profile_name}")

            # Refresh tabs
            self._claude_tab.refresh()
            self._usage_stats_tab.refresh()

            logger.info(f"Quick switched to Claude profile: {profile_name}")

        except Exception as e:
            logger.error(f"Failed to quick switch Claude: {e}", exc_info=True)
            from ui.widgets.toast import show_toast
            show_toast(self, f"切换失败: {e}", is_error=True)

    def _quick_switch_codex(self, profile_name: str):
        """Quick switch Codex profile."""
        if profile_name == "Codex" or profile_name == "无配置":
            return

        try:
            from core import switcher
            from core.usage_recorder import usage_recorder
            from ui.widgets.toast import show_toast

            switcher.switch_codex_profile(profile_name)
            usage_recorder.start_session(profile_name, "codex")

            show_toast(self, f"已切换到: {profile_name}")
            self._status.configure(text=f"已切换到 Codex: {profile_name}")

            # Refresh tabs
            self._codex_tab.refresh()
            self._usage_stats_tab.refresh()

            logger.info(f"Quick switched to Codex profile: {profile_name}")

        except Exception as e:
            logger.error(f"Failed to quick switch Codex: {e}", exc_info=True)
            from ui.widgets.toast import show_toast
            show_toast(self, f"切换失败: {e}", is_error=True)

    def _on_closing(self):
        """Handle window close event - minimize to tray instead of exit."""
        if not self.tray_manager.is_available():
            self._exit_app()
            return
        self.withdraw()  # Hide window
        logger.info("Window minimized to tray")

    def _show_window(self, icon=None, item=None):
        """Show the main window from tray."""
        self.deiconify()  # Show window
        self.lift()  # Bring to front
        self.focus_force()  # Give focus
        logger.info("Window restored from tray")

    def _exit_app(self):
        """Exit the application completely."""
        logger.info("Exiting application")
        self.tray_manager.stop()
        self.quit()
        self.destroy()

    def refresh_all(self):
        """Refresh all tabs."""
        self._claude_tab.refresh()
        self._codex_tab.refresh()
        self._browser_tab.refresh()
        self._ssh_tab.refresh()
        self._common_tab.refresh()
        self._usage_stats_tab.refresh()
        self._backup_tab.refresh()
        self._log_viewer_tab.refresh()
        self._status.configure(text="已刷新全部配置")

        # Update tray menu to reflect changes
        if self.tray_manager.is_running():
            self.tray_manager.update_menu()

        # Reload quick switch profiles
        self._load_quick_switch_profiles()

    def _show_health_check(self):
        """显示健康检查对话框"""
        from ui.dialogs.health_check_dialog import HealthCheckDialog
        dialog = HealthCheckDialog(self)
        dialog.focus()
