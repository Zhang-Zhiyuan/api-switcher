import importlib
import logging
import os
import threading

import customtkinter as ctk
from ui.theme import COLORS, bind_wraplength, button_style, combo_style, font
from core.tray_manager import TrayManager

logger = logging.getLogger(__name__)
ENV_TAB_LABEL = "环境变量"
PROXY_QUALITY_DIALOG_LABEL = "代理质量检测"
ENV_TAB_BUTTON_TEXT = "HF_TOKEN 等"
QUICK_SWITCH_TITLE = "快速切换 API"
CLAUDE_QUICK_SWITCH_LABEL = "Claude Code 使用"
CODEX_QUICK_SWITCH_LABEL = "Codex CLI 使用"
TAB_SPECS = [
    ("Claude Code", "_claude_tab", "ui.tabs.claude_tab", "ClaudeTab", True),
    ("Codex CLI", "_codex_tab", "ui.tabs.codex_tab", "CodexTab", False),
    (ENV_TAB_LABEL, "_env_tab", "ui.tabs.env_tab", "EnvTab", False),
    ("浏览器 Profile", "_browser_tab", "ui.tabs.browser_tab", "BrowserTab", False),
    ("会话迁移", "_session_migration_tab", "ui.tabs.session_migration_tab", "SessionMigrationTab", False),
    ("SSH 服务器", "_ssh_tab", "ui.tabs.ssh_tab", "SSHTab", False),
    ("Win11 代理", "_local_proxy_tab", "ui.tabs.local_proxy_tab", "LocalProxyTab", False),
    ("通用设置", "_common_tab", "ui.tabs.common_tab", "CommonTab", False),
    ("使用统计", "_usage_stats_tab", "ui.tabs.usage_stats_tab", "UsageStatsTab", False),
    ("备份管理", "_backup_tab", "ui.tabs.backup_tab", "BackupTab", False),
    ("日志查看器", "_log_viewer_tab", "ui.tabs.log_viewer_tab", "LogViewerTab", False),
]


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
        self._force_exit_timer_started = False
        self._proxy_quality_dialog = None
        self._tab_frames = {}
        self._tab_class_cache = {}
        self._tab_class_cache_lock = threading.RLock()
        self._lazy_tab_preload_started = False
        self._quick_switch_load_generation = 0
        self._tab_specs = {label: (attr, module_name, class_name, eager) for label, attr, module_name, class_name, eager in TAB_SPECS}
        for _label, attr, _module_name, _class_name, _eager in TAB_SPECS:
            setattr(self, attr, None)

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
            text=QUICK_SWITCH_TITLE,
            text_color=COLORS["muted"],
            font=font(11, "bold"),
        ).pack(side="left", padx=(0, 10))

        # Claude quick switch
        claude_switch_group = ctk.CTkFrame(switch_frame, fg_color="transparent")
        claude_switch_group.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(
            claude_switch_group,
            text=CLAUDE_QUICK_SWITCH_LABEL,
            text_color=COLORS["muted_soft"],
            font=font(10),
            anchor="w",
        ).pack(anchor="w", pady=(0, 2))
        self.claude_switch = ctk.CTkComboBox(
            claude_switch_group,
            width=172,
            command=self._quick_switch_claude,
            **combo_style(),
        )
        self.claude_switch.pack(anchor="w")
        self.claude_switch.set("Claude API")

        # Codex quick switch
        codex_switch_group = ctk.CTkFrame(switch_frame, fg_color="transparent")
        codex_switch_group.pack(side="left")
        ctk.CTkLabel(
            codex_switch_group,
            text=CODEX_QUICK_SWITCH_LABEL,
            text_color=COLORS["muted_soft"],
            font=font(10),
            anchor="w",
        ).pack(anchor="w", pady=(0, 2))
        self.codex_switch = ctk.CTkComboBox(
            codex_switch_group,
            width=172,
            command=self._quick_switch_codex,
            **combo_style(),
        )
        self.codex_switch.pack(anchor="w")
        self.codex_switch.set("Codex API")

        # 按钮区域
        button_group = ctk.CTkFrame(action_panel, fg_color="transparent")
        button_group.pack(side="right", padx=(0, 12), pady=9)
        ctk.CTkButton(
            button_group,
            text=ENV_TAB_BUTTON_TEXT,
            width=108,
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
            command=self._on_tab_changed,
        )
        self._tabview.pack(fill="both", expand=True)

        for label, _attr, _module_name, _class_name, eager in TAB_SPECS:
            self._tab_frames[label] = self._tabview.add(label)
            if not eager:
                self._install_tab_placeholder(label)

        for label, _attr, _module_name, _class_name, eager in TAB_SPECS:
            if eager:
                self._ensure_tab(label)

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

        self.claude_switch.configure(values=["正在加载..."], state="disabled")
        self.codex_switch.configure(values=["正在加载..."], state="disabled")
        self.after(20, self._load_quick_switch_profiles)
        self.after(50, self._start_tray_icon)
        self.after(900, self._auto_start_local_proxy)
        self.after(1200, self._preload_lazy_tab_classes)

    def _install_tab_placeholder(self, label: str):
        frame = self._tab_frames.get(label)
        if frame is None:
            return
        placeholder = ctk.CTkFrame(frame, fg_color="transparent")
        placeholder.pack(fill="both", expand=True, padx=24, pady=24)
        ctk.CTkLabel(
            placeholder,
            text=f"{label} 尚未加载",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(pady=(80, 6))
        ctk.CTkLabel(
            placeholder,
            text="切换到此页时会自动加载，以缩短启动等待时间。",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(pady=(0, 12))
        ctk.CTkButton(
            placeholder,
            text="加载",
            width=96,
            command=lambda name=label: self._ensure_tab(name),
            **button_style("secondary"),
        ).pack()

    def _set_app_status(self, message: str):
        status = getattr(self, "_status", None)
        if status is None:
            return
        try:
            status.configure(text=message)
        except Exception as exc:
            logger.debug("Failed to update status bar: %s", exc)

    def _show_tab_loading(self, label: str):
        frame = self._tab_frames.get(label)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        panel = ctk.CTkFrame(frame, fg_color="transparent")
        panel.pack(fill="both", expand=True, padx=24, pady=24)
        ctk.CTkLabel(
            panel,
            text=f"正在加载 {label}",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(pady=(90, 6))
        ctk.CTkLabel(
            panel,
            text="首次打开会初始化相关模块，请稍候。",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack()
        self._set_app_status(f"正在加载 {label}...")
        try:
            self.update_idletasks()
        except Exception as exc:
            logger.debug("Failed to render tab loading state: %s", exc)

    def _show_tab_error(self, label: str, error: Exception):
        frame = self._tab_frames.get(label)
        if frame is None:
            return
        for child in frame.winfo_children():
            child.destroy()
        detail = str(error).strip().splitlines()[0] if str(error).strip() else error.__class__.__name__
        if len(detail) > 260:
            detail = detail[:257] + "..."
        panel = ctk.CTkFrame(
            frame,
            fg_color=COLORS["surface_alt"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        panel.pack(fill="x", padx=28, pady=70)
        ctk.CTkLabel(
            panel,
            text=f"{label} 加载失败",
            text_color=COLORS["danger"],
            font=font(16, "bold"),
        ).pack(anchor="w", padx=18, pady=(16, 4))
        ctk.CTkLabel(
            panel,
            text=detail,
            text_color=COLORS["muted"],
            font=font(12),
            justify="left",
            anchor="w",
            wraplength=780,
        ).pack(fill="x", padx=18, pady=(0, 14))
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkButton(
            actions,
            text="重试加载",
            width=104,
            command=lambda name=label: self._ensure_tab(name),
            **button_style("primary"),
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text="回到首页",
            width=96,
            command=lambda: self._tabview.set("Claude Code"),
            **button_style("secondary"),
        ).pack(side="left", padx=(8, 0))
        self._set_app_status(f"{label} 加载失败，可点击重试加载")

    def _ensure_tab(self, label: str):
        spec = self._tab_specs.get(label)
        frame = self._tab_frames.get(label)
        if not spec or frame is None:
            return None

        attr, module_name, class_name, _eager = spec
        existing = getattr(self, attr, None)
        try:
            if existing is not None and existing.winfo_exists():
                return existing
        except Exception:
            pass

        self._show_tab_loading(label)
        try:
            tab_class = self._resolve_tab_class(label, module_name, class_name)
            for child in frame.winfo_children():
                child.destroy()
            tab = tab_class(frame)
            tab.pack(fill="both", expand=True)
            setattr(self, attr, tab)
            self._set_app_status(f"已加载 {label}")
            logger.debug("Loaded tab: %s", label)
            return tab
        except Exception as e:
            setattr(self, attr, None)
            logger.error("Failed to load tab %s: %s", label, e, exc_info=True)
            self._show_tab_error(label, e)
            return None

    def _resolve_tab_class(self, label: str, module_name: str, class_name: str):
        with self._tab_class_cache_lock:
            tab_class = self._tab_class_cache.get(label)
            if tab_class is not None:
                return tab_class

        module = importlib.import_module(module_name)
        tab_class = getattr(module, class_name)
        with self._tab_class_cache_lock:
            return self._tab_class_cache.setdefault(label, tab_class)

    def _preload_lazy_tab_classes(self):
        if self._exit_requested or self._lazy_tab_preload_started:
            return
        self._lazy_tab_preload_started = True
        priority = {
            "Win11 代理": 0,
            "SSH 服务器": 1,
            "Codex CLI": 2,
            ENV_TAB_LABEL: 3,
        }
        specs = [
            (priority.get(label, 50), label, module_name, class_name)
            for label, _attr, module_name, class_name, eager in TAB_SPECS
            if not eager
        ]
        specs.sort(key=lambda item: (item[0], item[1]))

        def run():
            for _order, label, module_name, class_name in specs:
                if self._exit_requested:
                    return
                with self._tab_class_cache_lock:
                    if label in self._tab_class_cache:
                        continue
                try:
                    self._resolve_tab_class(label, module_name, class_name)
                    logger.debug("Preloaded lazy tab class: %s", label)
                except Exception as exc:
                    logger.debug("Lazy tab preload skipped for %s: %s", label, exc)

        threading.Thread(target=run, name="lazy-tab-preload", daemon=True).start()

    def _on_tab_changed(self):
        self._ensure_tab(self._tabview.get())

    def _loaded_tab(self, attr: str):
        tab = getattr(self, attr, None)
        if tab is None:
            return None
        try:
            return tab if tab.winfo_exists() else None
        except Exception:
            return None

    def _refresh_loaded_tab(self, attr: str):
        tab = self._loaded_tab(attr)
        if tab and hasattr(tab, "refresh"):
            tab.refresh()

    def _start_tray_icon(self):
        if self._exit_requested:
            return
        if self.tray_manager.is_available():
            self.tray_manager.start()
            logger.info("Tray icon started")
        else:
            logger.info("Tray icon disabled: pystray is not installed")

    def _auto_start_local_proxy(self):
        if self._exit_requested:
            return

        def run():
            try:
                from core import local_proxy

                if not local_proxy.local_proxy_start_on_login_enabled():
                    return
                message = local_proxy.auto_start_local_ai_proxy_if_enabled()
                logger.info("Local proxy auto-start: %s", message)

                def update_status():
                    if self._exit_requested:
                        return
                    self._status.configure(text=message)
                    self._refresh_loaded_tab("_local_proxy_tab")

                self._run_on_ui_thread(update_status)
            except Exception as e:
                logger.error("Failed to auto-start local proxy: %s", e, exc_info=True)
                error_message = str(e)

                def update_error():
                    if not self._exit_requested:
                        self._status.configure(text=f"Win11 本机代理自启失败: {error_message}")

                self._run_on_ui_thread(update_error)

        threading.Thread(target=run, daemon=True).start()

    def _load_quick_switch_profiles(self):
        """Load profiles for quick switch menus."""
        self._quick_switch_load_generation += 1
        generation = self._quick_switch_load_generation

        def run():
            try:
                from core import profile_manager

                payload = {
                    "ok": True,
                    "error": "",
                    "claude_names": [p.name for p in profile_manager.list_switchable_claude_profiles()],
                    "claude_current": profile_manager.get_current_claude_name()
                    or profile_manager.get_active_claude_name(),
                    "codex_names": [p.name for p in profile_manager.list_switchable_codex_profiles()],
                    "codex_current": profile_manager.get_current_codex_name()
                    or profile_manager.get_active_codex_name(),
                }
            except Exception as e:
                payload = {"ok": False, "error": str(e)}

            def finish():
                if generation != self._quick_switch_load_generation or self._exit_requested:
                    return
                if not payload["ok"]:
                    logger.error("Failed to load quick switch profiles: %s", payload["error"])
                    self.claude_switch.configure(values=["加载失败"], state="disabled")
                    self.claude_switch.set("加载失败")
                    self.codex_switch.configure(values=["加载失败"], state="disabled")
                    self.codex_switch.set("加载失败")
                    return
                self._apply_quick_switch_profiles(
                    payload["claude_names"],
                    payload["claude_current"],
                    payload["codex_names"],
                    payload["codex_current"],
                )

            self._run_on_ui_thread(finish)

        threading.Thread(target=run, daemon=True).start()

    def _apply_quick_switch_profiles(self, claude_names, claude_current, codex_names, codex_current):
        if claude_names:
            self.claude_switch.configure(values=claude_names, state="normal")
            self.claude_switch.set(claude_current if claude_current in claude_names else claude_names[0])
        else:
            self.claude_switch.configure(values=["暂无 Claude API 配置"], state="disabled")
            self.claude_switch.set("暂无 Claude API 配置")

        if codex_names:
            self.codex_switch.configure(values=codex_names, state="normal")
            self.codex_switch.set(codex_current if codex_current in codex_names else codex_names[0])
        else:
            self.codex_switch.configure(values=["暂无 Codex API 配置"], state="disabled")
            self.codex_switch.set("暂无 Codex API 配置")

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

                self._refresh_loaded_tab("_claude_tab")
                self._refresh_loaded_tab("_usage_stats_tab")
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

                self._refresh_loaded_tab("_codex_tab")
                self._refresh_loaded_tab("_usage_stats_tab")
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
        self._schedule_force_exit_fallback()
        self._shutdown_runtime_resources()
        try:
            self.quit()
        except Exception as e:
            logger.debug("Failed to quit mainloop cleanly: %s", e)
        try:
            self.destroy()
        except Exception as e:
            logger.debug("Failed to destroy main window cleanly: %s", e)

    def _schedule_force_exit_fallback(self) -> None:
        """Ensure a requested exit cannot leave the Python process stuck forever."""
        if self._force_exit_timer_started:
            return
        self._force_exit_timer_started = True

        def force_exit_if_still_alive() -> None:
            logger.error("Forced process exit after graceful shutdown timeout")
            os._exit(0)

        timer = threading.Timer(6.0, force_exit_if_still_alive)
        timer.daemon = True
        timer.start()

    def _shutdown_runtime_resources(self) -> None:
        """Best-effort cleanup for resources that can keep the process alive."""
        try:
            if self._close_dialog and self._close_dialog.winfo_exists():
                self._close_dialog.destroy()
        except Exception as e:
            logger.debug("Failed to close exit dialog: %s", e)
        self._close_dialog = None

        for _label, attr, _module_name, _class_name, _eager in TAB_SPECS:
            tab = getattr(self, attr, None)
            if tab is None:
                continue
            try:
                if tab.winfo_exists():
                    tab.destroy()
            except Exception as e:
                logger.debug("Failed to destroy tab %s: %s", attr, e)

        try:
            self.tray_manager.stop()
        except Exception as e:
            logger.warning("Failed to stop tray manager during exit: %s", e)

        try:
            from core import local_proxy

            if local_proxy.local_proxy_keep_running_on_exit_enabled():
                logger.info("Win11 local proxy configured to keep running after app exit")
            else:
                message = local_proxy.stop_local_ai_proxy(restore_settings=True)
                logger.info("Stopped Win11 local proxy during app exit: %s", message)
        except Exception as e:
            logger.warning("Failed to apply Win11 local proxy exit policy: %s", e)

        try:
            from core.ssh_manager import ssh_manager

            ssh_manager.disconnect_all()
        except Exception as e:
            logger.warning("Failed to disconnect SSH sessions during exit: %s", e)

    def _on_startup_changed_from_tray(self):
        def refresh_startup():
            self._refresh_loaded_tab("_common_tab")

        self._run_on_ui_thread(refresh_startup)

    def _run_on_ui_thread(self, callback):
        try:
            if self.winfo_exists():
                self.after(0, callback)
        except Exception as e:
            logger.debug("Failed to schedule UI callback: %s", e)

    def _show_env_tab(self):
        self._tabview.set(ENV_TAB_LABEL)
        tab = self._ensure_tab(ENV_TAB_LABEL)
        if tab:
            tab.refresh()
        self._status.configure(text="已打开环境变量管理")

    def _show_proxy_quality_dialog(self):
        try:
            existing = self._proxy_quality_dialog
            if existing is not None and existing.winfo_exists():
                existing.lift()
                existing.focus()
                return existing
        except Exception:
            self._proxy_quality_dialog = None

        try:
            from ui.dialogs.proxy_quality_dialog import ProxyQualityDialog

            dialog = ProxyQualityDialog(self, on_close=lambda: setattr(self, "_proxy_quality_dialog", None))
            self._proxy_quality_dialog = dialog
            self._status.configure(text="已打开代理质量检测")
            return dialog
        except Exception as exc:
            logger.error("Failed to open proxy quality dialog: %s", exc, exc_info=True)
            self._status.configure(text=f"代理质量检测打开失败: {exc}")
            try:
                from ui.widgets.toast import show_toast

                show_toast(self, f"代理质量检测打开失败: {exc}", is_error=True)
            except Exception:
                pass
            return None

    def refresh_all(self):
        """Refresh all tabs."""
        self._status.configure(text="正在刷新全部 API 配置和账号状态...")
        self.update_idletasks()
        for _label, attr, _module_name, _class_name, _eager in TAB_SPECS:
            tab = self._loaded_tab(attr)
            if tab and hasattr(tab, "refresh"):
                tab.refresh()
        self._status.configure(text="已刷新已加载页面和快捷菜单")

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
