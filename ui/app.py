import importlib
import logging
import os
import queue
import threading
import time

import customtkinter as ctk
from ui.theme import COLORS, bind_wraplength, button_style, combo_style, fit_window_to_screen, font, recent_user_scroll
from ui.widgets.adaptive_tab_bar import AdaptiveTabBar

logger = logging.getLogger(__name__)
ENV_TAB_LABEL = "环境变量"
PROXY_QUALITY_DIALOG_LABEL = "代理质量检测"
ENV_TAB_BUTTON_TEXT = "HF_TOKEN 等"
QUICK_SWITCH_TITLE = "快速切换 API"
CLAUDE_QUICK_SWITCH_LABEL = "Claude Code 使用"
CODEX_QUICK_SWITCH_LABEL = "Codex CLI 使用"
DEFAULT_TAB_LABEL = "Claude Code"
DEFAULT_TAB_PRELOAD_MODE = "priority"
DEFAULT_TAB_WARMUP_MODE = "0"
TAB_CLASS_PRELOAD_START_MS = 4200
TAB_CLASS_PRELOAD_RETRY_MS = 700
TAB_WARMUP_START_MS = 2600
TAB_WARMUP_STEP_MS = 750
TAB_WARMUP_SCROLL_IDLE_MS = 1000
TAB_WARMUP_INTERACTION_IDLE_MS = 1800
TAB_WARMUP_RETRY_MS = 320
UI_CALLBACK_IDLE_POLL_MS = 16
UI_CALLBACK_BUSY_POLL_MS = 8
UI_CALLBACK_BATCH_LIMIT = 4
UI_CALLBACK_TIME_BUDGET_MS = 5
UI_CALLBACK_SCROLL_IDLE_MS = 240
UI_CALLBACK_SCROLL_RETRY_MS = 16
QUICK_SWITCH_INITIAL_LOAD_MS = 2200
MAIN_LAYOUT_WIDE_MIN_WIDTH = 1020
MAIN_LAYOUT_COMPACT_MIN_WIDTH = 620
TAB_SPECS = [
    ("Claude Code", "_claude_tab", "ui.tabs.claude_tab", "ClaudeTab", False),
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


def main_layout_mode(width: int) -> str:
    """Choose the main header layout from the available logical width."""

    if width >= MAIN_LAYOUT_WIDE_MIN_WIDTH:
        return "wide"
    if width >= MAIN_LAYOUT_COMPACT_MIN_WIDTH:
        return "compact"
    return "narrow"


class _LazyTrayManager:
    """Delay tray imports until tray support is actually needed."""

    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self._manager = None
        self._lock = threading.RLock()

    def _load(self):
        manager = self._manager
        if manager is not None:
            return manager
        with self._lock:
            if self._manager is None:
                from core.tray_manager import TrayManager

                self._manager = TrayManager(**self._kwargs)
            return self._manager

    def is_running(self) -> bool:
        manager = self._manager
        return bool(manager and manager.is_running())

    def is_available(self) -> bool:
        return self._load().is_available()

    def start(self) -> None:
        self._load().start()

    def stop(self) -> None:
        manager = self._manager
        if manager is not None:
            manager.stop()

    def update_menu(self) -> None:
        manager = self._manager
        if manager is not None:
            manager.update_menu()

    def notify(self, *args, **kwargs) -> None:
        manager = self._manager
        if manager is not None:
            manager.notify(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._load(), name)


class App(ctk.CTk):
    """Main application window."""

    def __init__(self, start_minimized: bool = False):
        super().__init__()

        self.title("API 配置切换器")
        self.geometry("1120x760")
        self.minsize(480, 460)
        fit_window_to_screen(
            self,
            preferred_size=(1120, 760),
            minimum_size=(480, 460),
        )
        self.configure(fg_color=COLORS["app_bg"])
        self._exit_requested = False
        self._ui_thread_id = threading.get_ident()
        self._ui_callback_queue = queue.Queue()
        self._ui_callback_after_id = None
        self._tray_hint_shown = False
        self._tray_starting = False
        self._close_dialog = None
        self._force_exit_timer_started = False
        self._proxy_quality_dialog = None
        self._tab_frames = {}
        self._tab_class_cache = {}
        self._tab_class_cache_lock = threading.RLock()
        self._lazy_tab_preload_started = False
        self._lazy_tab_preload_after_id = None
        self._lazy_tab_warmup_started = False
        self._pending_tab_warmup_after_id = None
        self._tab_warmup_queue = []
        self._initial_tab_load_started = False
        self._pending_tab_load_after_ids = {}
        self._tab_class_loading = set()
        self._tab_load_generations = {}
        self._last_user_interaction_at = 0.0
        self._quick_switch_load_generation = 0
        self._quick_switch_load_after_id = None
        self._quick_switch_loading = False
        self._quick_switch_reload_pending = False
        self._switch_preview_generation = 0
        self._main_layout_after_id = None
        self._main_layout_mode = None
        self._tab_specs = {label: (attr, module_name, class_name, eager) for label, attr, module_name, class_name, eager in TAB_SPECS}
        for _label, attr, _module_name, _class_name, _eager in TAB_SPECS:
            setattr(self, attr, None)

        # Initialize tray manager
        self.tray_manager = _LazyTrayManager(
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
        self.bind_all("<ButtonPress>", self._mark_user_interaction, add="+")
        self.bind_all("<KeyPress>", self._mark_user_interaction, add="+")

        self._shell = ctk.CTkFrame(self, fg_color="transparent")
        self._shell.pack(fill="both", expand=True, padx=20, pady=(18, 14))

        # Top bar
        topbar = ctk.CTkFrame(self._shell, fg_color="transparent")
        topbar.pack(fill="x", pady=(0, 12))

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

        self._action_panel = ctk.CTkFrame(
            topbar,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        self._action_panel.pack(fill="x", pady=(10, 0))
        self._action_panel.grid_columnconfigure(0, weight=1)

        # Quick switch menu
        self._switch_frame = ctk.CTkFrame(self._action_panel, fg_color="transparent")
        self._switch_frame.grid(row=0, column=0, sticky="w", padx=(12, 8), pady=9)

        self._switch_title = ctk.CTkLabel(
            self._switch_frame,
            text=QUICK_SWITCH_TITLE,
            text_color=COLORS["muted"],
            font=font(11, "bold"),
        )
        self._switch_title.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))

        # Claude quick switch
        self._claude_switch_group = ctk.CTkFrame(self._switch_frame, fg_color="transparent")
        self._claude_switch_group.grid(row=0, column=1, sticky="w", padx=(0, 10))
        ctk.CTkLabel(
            self._claude_switch_group,
            text=CLAUDE_QUICK_SWITCH_LABEL,
            text_color=COLORS["muted_soft"],
            font=font(10),
            anchor="w",
        ).pack(anchor="w", pady=(0, 2))
        self.claude_switch = ctk.CTkComboBox(
            self._claude_switch_group,
            width=172,
            command=self._quick_switch_claude,
            **combo_style(),
        )
        self.claude_switch.pack(anchor="w")
        self.claude_switch.set("Claude API")

        # Codex quick switch
        self._codex_switch_group = ctk.CTkFrame(self._switch_frame, fg_color="transparent")
        self._codex_switch_group.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(
            self._codex_switch_group,
            text=CODEX_QUICK_SWITCH_LABEL,
            text_color=COLORS["muted_soft"],
            font=font(10),
            anchor="w",
        ).pack(anchor="w", pady=(0, 2))
        self.codex_switch = ctk.CTkComboBox(
            self._codex_switch_group,
            width=172,
            command=self._quick_switch_codex,
            **combo_style(),
        )
        self.codex_switch.pack(anchor="w")
        self.codex_switch.set("Codex API")

        # 按钮区域
        self._button_group = ctk.CTkFrame(self._action_panel, fg_color="transparent")
        self._button_group.grid(row=0, column=1, sticky="e", padx=(0, 12), pady=9)
        self._global_action_buttons = []
        env_button = ctk.CTkButton(
            self._button_group,
            text=ENV_TAB_BUTTON_TEXT,
            width=108,
            command=self._show_env_tab,
            **button_style("primary"),
        )
        self._global_action_buttons.append(env_button)

        health_button = ctk.CTkButton(
            self._button_group,
            text="健康检查",
            width=96,
            command=self._show_health_check,
            **button_style("accent"),
        )
        self._global_action_buttons.append(health_button)

        rollback_button = ctk.CTkButton(
            self._button_group,
            text="回滚上次",
            width=96,
            command=self._restore_latest_backup,
            **button_style("warning"),
        )
        self._global_action_buttons.append(rollback_button)

        refresh_button = ctk.CTkButton(
            self._button_group,
            text="刷新全部",
            width=96,
            command=self.refresh_all,
            **button_style("secondary"),
        )
        self._global_action_buttons.append(refresh_button)
        for index, button in enumerate(self._global_action_buttons):
            button.grid(row=0, column=index, sticky="ew", padx=(8 if index else 0, 0))

        # The stock CTkTabview header is a single non-scrollable row. A
        # wrapping selector keeps every destination reachable at any width.
        self._tab_navigation = AdaptiveTabBar(
            self._shell,
            [label for label, *_spec in TAB_SPECS],
            command=self._select_tab_from_navigation,
        )
        self._tab_navigation.pack(fill="x", pady=(0, 4))

        # Tab view
        self._tabview = ctk.CTkTabview(
            self._shell,
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
        self._hide_native_tab_header()
        self._tab_navigation.set(self._tabview.get() or DEFAULT_TAB_LABEL)

        # Status bar
        footer = ctk.CTkFrame(
            self._shell,
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
        self.bind("<Configure>", self._schedule_main_layout, add="+")
        self.after_idle(self._apply_main_layout)
        self._schedule_ui_callback_pump(delay_ms=UI_CALLBACK_IDLE_POLL_MS)

        self.claude_switch.configure(values=["正在加载..."], state="disabled")
        self.codex_switch.configure(values=["正在加载..."], state="disabled")
        self.after(QUICK_SWITCH_INITIAL_LOAD_MS, lambda: self._load_quick_switch_profiles(delay_ms=0))
        self.after(50, self._start_tray_icon)
        if not self._start_minimized_to_tray:
            self.after(90, self._schedule_initial_tab_load)
        self.after(900, self._auto_start_local_proxy)
        preload_mode = os.environ.get("API_SWITCHER_PRELOAD_TABS", DEFAULT_TAB_PRELOAD_MODE).strip().lower()
        if preload_mode != "0":
            self._schedule_lazy_tab_preload(preload_mode, delay_ms=TAB_CLASS_PRELOAD_START_MS)
        warmup_mode = os.environ.get("API_SWITCHER_WARM_TABS", DEFAULT_TAB_WARMUP_MODE).strip().lower()
        if warmup_mode != "0":
            self.after(
                TAB_WARMUP_START_MS,
                lambda mode=warmup_mode: self._start_lazy_tab_warmup(priority_only=mode not in {"1", "all"}),
            )

    def _logical_main_width(self) -> int:
        width = self.winfo_width()
        try:
            scaling = float(self._get_window_scaling())
        except (AttributeError, TypeError, ValueError):
            scaling = 1.0
        if scaling > 0:
            width = round(width / scaling)
        return max(1, width)

    def _schedule_main_layout(self, event=None) -> None:
        if event is not None and getattr(event, "widget", self) is not self:
            return
        if self._main_layout_after_id is not None:
            return
        try:
            self._main_layout_after_id = self.after_idle(self._apply_main_layout)
        except Exception:
            self._main_layout_after_id = None

    def _apply_main_layout(self) -> None:
        self._main_layout_after_id = None
        mode = main_layout_mode(self._logical_main_width())
        if mode == self._main_layout_mode:
            return
        self._main_layout_mode = mode

        for column in range(4):
            self._button_group.grid_columnconfigure(column, weight=0, minsize=0, uniform="")
            self._switch_frame.grid_columnconfigure(column, weight=0, minsize=0, uniform="")

        if mode == "wide":
            self._shell.pack_configure(padx=20, pady=(18, 14))
            self._action_panel.grid_columnconfigure(0, weight=1)
            self._action_panel.grid_columnconfigure(1, weight=0)
            self._switch_frame.grid_configure(row=0, column=0, columnspan=1, sticky="w", padx=(12, 8), pady=9)
            self._button_group.grid_configure(row=0, column=1, columnspan=1, sticky="e", padx=(0, 12), pady=9)
            self._switch_title.grid_configure(row=0, column=0, rowspan=1, columnspan=1, sticky="w", padx=(0, 10), pady=(17, 0))
            self._claude_switch_group.grid_configure(row=0, column=1, sticky="w", padx=(0, 10))
            self._codex_switch_group.grid_configure(row=0, column=2, sticky="w", padx=0)
            action_columns = 4
        else:
            shell_padding = 12 if mode == "compact" else 8
            self._shell.pack_configure(padx=shell_padding, pady=(12, 10))
            self._action_panel.grid_columnconfigure(0, weight=1)
            self._action_panel.grid_columnconfigure(1, weight=1)
            self._switch_frame.grid_configure(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(9, 4))
            self._button_group.grid_configure(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(3, 9))
            if mode == "compact":
                self._switch_title.grid_configure(row=0, column=0, rowspan=1, columnspan=1, sticky="w", padx=(0, 10), pady=(17, 0))
                self._claude_switch_group.grid_configure(row=0, column=1, sticky="w", padx=(0, 10))
                self._codex_switch_group.grid_configure(row=0, column=2, sticky="w", padx=0)
                action_columns = 4
            else:
                self._switch_frame.grid_columnconfigure(0, weight=1)
                self._switch_frame.grid_columnconfigure(1, weight=1)
                self._switch_title.grid_configure(row=0, column=0, rowspan=1, columnspan=2, sticky="w", padx=0, pady=(0, 3))
                self._claude_switch_group.grid_configure(row=1, column=0, sticky="w", padx=(0, 6))
                self._codex_switch_group.grid_configure(row=1, column=1, sticky="w", padx=0)
                action_columns = 2

        for column in range(action_columns):
            self._button_group.grid_columnconfigure(column, weight=1, uniform="global-actions")
        for index, button in enumerate(self._global_action_buttons):
            button.grid_configure(
                row=index // action_columns,
                column=index % action_columns,
                sticky="ew",
                padx=(0 if index % action_columns == 0 else 6, 0),
                pady=(0 if index < action_columns else 5, 0),
            )

    def _hide_native_tab_header(self) -> None:
        segmented_button = getattr(self._tabview, "_segmented_button", None)
        if segmented_button is not None:
            segmented_button.grid_remove()
        # Collapse the three rows reserved by CTkTabview for its stock header.
        for row in range(3):
            self._tabview.grid_rowconfigure(row, minsize=0, weight=0)

    def _select_tab_from_navigation(self, label: str) -> None:
        if label not in self._tab_frames:
            return
        if self._tabview.get() != label:
            self._tabview.set(label)
        self._on_tab_changed()

    def _select_tab(self, label: str) -> None:
        if label not in self._tab_frames:
            return
        self._tab_navigation.set(label)
        if self._tabview.get() != label:
            self._tabview.set(label)
        self._on_tab_changed()

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
            command=lambda name=label: self._schedule_tab_load(name),
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
            command=lambda name=label: self._schedule_tab_load(name),
            **button_style("primary"),
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text="回到首页",
            width=96,
            command=lambda: self._select_tab("Claude Code"),
            **button_style("secondary"),
        ).pack(side="left", padx=(8, 0))
        self._set_app_status(f"{label} 加载失败，可点击重试加载")

    def _tab_is_loaded(self, attr: str) -> bool:
        existing = getattr(self, attr, None)
        try:
            return existing is not None and existing.winfo_exists()
        except Exception:
            return False

    def _ensure_tab(self, label: str):
        spec = self._tab_specs.get(label)
        frame = self._tab_frames.get(label)
        if not spec or frame is None:
            return None

        attr, module_name, class_name, _eager = spec
        if self._tab_is_loaded(attr):
            return getattr(self, attr, None)

        self._show_tab_loading(label)
        try:
            tab_class = self._resolve_tab_class(label, module_name, class_name)
            return self._instantiate_tab_from_class(label, tab_class)
        except Exception as e:
            setattr(self, attr, None)
            logger.error("Failed to load tab %s: %s", label, e, exc_info=True)
            self._show_tab_error(label, e)
            return None

    def _instantiate_tab_from_class(self, label: str, tab_class, *, background: bool = False):
        spec = self._tab_specs.get(label)
        frame = self._tab_frames.get(label)
        if not spec or frame is None:
            return None
        attr, _module_name, _class_name, _eager = spec
        if self._tab_is_loaded(attr):
            return getattr(self, attr, None)
        for child in frame.winfo_children():
            child.destroy()
        tab = tab_class(frame)
        setattr(tab, "_api_switcher_tab_label", label)
        tab.pack(fill="both", expand=True)
        setattr(self, attr, tab)
        if not background or self._tabview.get() == label:
            self._set_app_status(f"已加载 {label}")
        logger.debug("Loaded tab: %s", label)
        return tab

    def _resolve_tab_class(self, label: str, module_name: str, class_name: str):
        with self._tab_class_cache_lock:
            tab_class = self._tab_class_cache.get(label)
            if tab_class is not None:
                return tab_class

        module = importlib.import_module(module_name)
        tab_class = getattr(module, class_name)
        with self._tab_class_cache_lock:
            return self._tab_class_cache.setdefault(label, tab_class)

    def _schedule_lazy_tab_preload(self, mode: str, delay_ms: int = TAB_CLASS_PRELOAD_RETRY_MS):
        if self._exit_requested or self._lazy_tab_preload_started or self._lazy_tab_preload_after_id:
            return

        def run_when_idle():
            self._lazy_tab_preload_after_id = None
            if self._exit_requested or self._lazy_tab_preload_started:
                return
            if (
                recent_user_scroll(self, idle_ms=TAB_WARMUP_SCROLL_IDLE_MS)
                or self._recent_user_interaction(idle_ms=TAB_WARMUP_INTERACTION_IDLE_MS)
                or self._ui_callback_queue_has_pending()
            ):
                self._schedule_lazy_tab_preload(mode, delay_ms=TAB_CLASS_PRELOAD_RETRY_MS)
                return
            self._preload_lazy_tab_classes(priority_only=mode != "1")

        try:
            self._lazy_tab_preload_after_id = self.after(max(1, int(delay_ms)), run_when_idle)
        except Exception:
            self._lazy_tab_preload_after_id = None

    def _preload_lazy_tab_classes(self, priority_only: bool = False):
        if self._exit_requested or self._lazy_tab_preload_started:
            return
        self._lazy_tab_preload_started = True
        priority = {
            "Win11 代理": 0,
            "SSH 服务器": 1,
            "Codex CLI": 2,
            ENV_TAB_LABEL: 3,
        }
        priority_labels = set(priority)
        specs = [
            (priority.get(label, 50), label, module_name, class_name)
            for label, _attr, module_name, class_name, eager in TAB_SPECS
            if not eager and (not priority_only or label in priority_labels)
        ]
        specs.sort(key=lambda item: (item[0], item[1]))

        def run():
            for _order, label, module_name, class_name in specs:
                if self._exit_requested:
                    return
                while (
                    not self._exit_requested
                    and (
                        self._recent_user_interaction(idle_ms=TAB_WARMUP_INTERACTION_IDLE_MS)
                        or self._ui_callback_queue_has_pending()
                    )
                ):
                    time.sleep(0.15)
                with self._tab_class_cache_lock:
                    if label in self._tab_class_cache:
                        continue
                try:
                    self._resolve_tab_class(label, module_name, class_name)
                    logger.debug("Preloaded lazy tab class: %s", label)
                    time.sleep(0.03)
                except Exception as exc:
                    logger.debug("Lazy tab preload skipped for %s: %s", label, exc)

        threading.Thread(target=run, name="lazy-tab-preload", daemon=True).start()

    def _start_lazy_tab_warmup(self, priority_only: bool = False):
        if self._exit_requested or self._lazy_tab_warmup_started:
            return
        self._lazy_tab_warmup_started = True
        current = self._tabview.get() or DEFAULT_TAB_LABEL
        priority = {
            "Win11 代理": 0,
            "SSH 服务器": 1,
            "浏览器 Profile": 2,
            "会话迁移": 3,
            "Codex CLI": 4,
            ENV_TAB_LABEL: 5,
            "通用设置": 6,
            "使用统计": 7,
            "备份管理": 8,
            "日志查看器": 9,
        }
        priority_labels = set(priority)
        queue_items = [
            (priority.get(label, 50), label)
            for label, _attr, _module_name, _class_name, eager in TAB_SPECS
            if not eager
            and label != current
            and (not priority_only or label in priority_labels)
        ]
        queue_items.sort(key=lambda item: (item[0], item[1]))
        self._tab_warmup_queue = [label for _order, label in queue_items]
        self._schedule_next_tab_warmup(0)

    def _schedule_next_tab_warmup(self, delay_ms: int = TAB_WARMUP_STEP_MS):
        if self._exit_requested or not self._tab_warmup_queue:
            self._pending_tab_warmup_after_id = None
            return

        def warm_next():
            self._pending_tab_warmup_after_id = None
            self._warm_next_lazy_tab()

        try:
            self._pending_tab_warmup_after_id = self.after(max(1, int(delay_ms)), warm_next)
        except Exception:
            self._pending_tab_warmup_after_id = None

    def _warm_next_lazy_tab(self):
        if self._exit_requested:
            return
        if (
            recent_user_scroll(self, idle_ms=TAB_WARMUP_SCROLL_IDLE_MS)
            or self._recent_user_interaction(idle_ms=TAB_WARMUP_INTERACTION_IDLE_MS)
            or self._ui_callback_queue_has_pending()
        ):
            self._schedule_next_tab_warmup(TAB_WARMUP_RETRY_MS)
            return
        while self._tab_warmup_queue:
            label = self._tab_warmup_queue.pop(0)
            spec = self._tab_specs.get(label)
            if not spec:
                continue
            attr, module_name, class_name, _eager = spec
            if self._tab_is_loaded(attr):
                continue
            if label in self._pending_tab_load_after_ids or label in self._tab_class_loading:
                self._tab_warmup_queue.append(label)
                self._schedule_next_tab_warmup(TAB_WARMUP_STEP_MS)
                return
            try:
                tab_class = self._resolve_tab_class(label, module_name, class_name)
                self._instantiate_tab_from_class(label, tab_class, background=True)
                logger.debug("Warmed lazy tab: %s", label)
            except Exception as exc:
                logger.debug("Lazy tab warmup skipped for %s: %s", label, exc, exc_info=True)
            self._schedule_next_tab_warmup(TAB_WARMUP_STEP_MS)
            return
        self._pending_tab_warmup_after_id = None

    def _schedule_initial_tab_load(self):
        if self._exit_requested or self._initial_tab_load_started:
            return
        self._initial_tab_load_started = True
        current = self._tabview.get() or DEFAULT_TAB_LABEL
        self._schedule_tab_load(current, delay_ms=10)

    def _schedule_tab_load(self, label: str, delay_ms: int = 25):
        if self._exit_requested:
            return
        spec = self._tab_specs.get(label)
        if not spec:
            return
        attr, _module_name, _class_name, _eager = spec
        if self._tab_is_loaded(attr):
            return
        if label in self._pending_tab_load_after_ids:
            return
        if label in self._tab_class_loading:
            self._show_tab_loading(label)
            return
        self._show_tab_loading(label)

        def load_now():
            self._pending_tab_load_after_ids.pop(label, None)
            if not self._exit_requested:
                self._load_tab_class_async(label)

        self._pending_tab_load_after_ids[label] = None
        try:
            after_id = self.after(max(1, int(delay_ms)), load_now)
            if label in self._pending_tab_load_after_ids:
                self._pending_tab_load_after_ids[label] = after_id
        except Exception:
            self._pending_tab_load_after_ids.pop(label, None)
            self._ensure_tab(label)

    def _load_tab_class_async(self, label: str):
        spec = self._tab_specs.get(label)
        if not spec or self._exit_requested:
            return
        attr, module_name, class_name, _eager = spec
        if self._tab_is_loaded(attr):
            return
        if label in self._tab_class_loading:
            return
        self._tab_class_loading.add(label)
        generation = self._tab_load_generations.get(label, 0) + 1
        self._tab_load_generations[label] = generation

        def worker():
            payload = {"ok": False, "tab_class": None, "error": None}
            try:
                payload["tab_class"] = self._resolve_tab_class(label, module_name, class_name)
                payload["ok"] = True
            except Exception as exc:
                payload["error"] = exc

            def finish():
                self._tab_class_loading.discard(label)
                if self._exit_requested or generation != self._tab_load_generations.get(label):
                    return
                if not payload["ok"]:
                    error = payload["error"] or RuntimeError("未知加载错误")
                    setattr(self, attr, None)
                    logger.error("Failed to load tab %s: %s", label, error)
                    self._show_tab_error(label, error)
                    return
                try:
                    self._instantiate_tab_from_class(label, payload["tab_class"])
                except Exception as exc:
                    setattr(self, attr, None)
                    logger.error("Failed to create tab %s: %s", label, exc, exc_info=True)
                    self._show_tab_error(label, exc)

            self._run_on_ui_thread(finish)

        threading.Thread(target=worker, name=f"tab-class-load-{label}", daemon=True).start()

    def _on_tab_changed(self):
        self._mark_user_interaction()
        label = self._tabview.get()
        navigation = self.__dict__.get("_tab_navigation")
        if navigation is not None:
            navigation.set(label)
        self._suspend_inactive_tab_work(label)
        self._schedule_tab_load(label, delay_ms=1)
        self._resume_active_tab_work(label)

    def _mark_user_interaction(self, _event=None) -> None:
        try:
            self._last_user_interaction_at = time.perf_counter()
        except Exception:
            pass

    def _recent_user_interaction(self, idle_ms: int = 700) -> bool:
        try:
            last_interaction = float(self.__dict__.get("_last_user_interaction_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            return False
        if last_interaction <= 0.0:
            return False
        return (time.perf_counter() - last_interaction) * 1000 < max(1, int(idle_ms))

    def _suspend_inactive_tab_work(self, active_label: str) -> None:
        for label, attr, _module_name, _class_name, _eager in TAB_SPECS:
            if label == active_label:
                continue
            tab = self._loaded_tab(attr)
            if tab is None:
                continue
            self._call_background_work_hook(tab, "_suspend_background_work", label)

    def _resume_active_tab_work(self, active_label: str) -> None:
        spec = self._tab_specs.get(active_label)
        if not spec:
            return
        tab = self._loaded_tab(spec[0])
        if tab is None:
            return
        self._call_background_work_hook(tab, "_resume_background_work", active_label)

    def _call_background_work_hook(self, tab, hook_name: str, label: str) -> None:
        for widget in self._iter_background_work_targets(tab):
            hook = getattr(widget, hook_name, None)
            if not callable(hook):
                continue
            try:
                hook()
            except Exception as exc:
                logger.debug("Failed to call %s for %s: %s", hook_name, label, exc)

    def _iter_background_work_targets(self, tab):
        provider = getattr(tab, "_iter_background_work_targets", None)
        if callable(provider):
            try:
                targets = list(provider())
            except Exception as exc:
                logger.debug("Failed to enumerate background work targets: %s", exc)
                targets = []
            if targets:
                seen = set()
                for target in targets:
                    marker = id(target)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    try:
                        if target is not None and target.winfo_exists():
                            yield target
                    except Exception:
                        continue
                return
        yield tab

    def _iter_widget_tree(self, widget):
        stack = [widget]
        seen = set()
        while stack:
            current = stack.pop()
            marker = id(current)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                if not current.winfo_exists():
                    continue
            except Exception:
                continue
            yield current
            try:
                children = list(current.winfo_children())
            except Exception:
                children = []
            stack.extend(reversed(children))

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
        if self._tray_starting:
            return
        self._tray_starting = True

        def run():
            try:
                if self._exit_requested:
                    return
                if self.tray_manager.is_running():
                    return
                if self.tray_manager.is_available():
                    if not self._exit_requested:
                        self.tray_manager.start()
                        logger.info("Tray icon started")
                else:
                    logger.info("Tray icon disabled: pystray is not installed")
            except Exception as exc:
                logger.error("Failed to start tray icon: %s", exc, exc_info=True)
            finally:
                self._tray_starting = False

        threading.Thread(target=run, name="tray-startup", daemon=True).start()

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

    def _load_quick_switch_profiles(self, delay_ms: int = 80):
        """Load profiles for quick switch menus."""
        self._load_quick_switch_profiles_delayed(delay_ms=delay_ms)

    def _load_quick_switch_profiles_delayed(self, delay_ms: int = 80):
        if self._exit_requested:
            return
        if self._quick_switch_load_after_id:
            return

        def start_load():
            self._quick_switch_load_after_id = None
            self._run_quick_switch_profile_load()

        try:
            self._quick_switch_load_after_id = self.after(max(0, int(delay_ms)), start_load)
        except Exception:
            self._quick_switch_load_after_id = None
            self._run_quick_switch_profile_load()

    def _run_quick_switch_profile_load(self):
        if self._exit_requested:
            return
        if self._quick_switch_loading:
            self._quick_switch_reload_pending = True
            return
        self._quick_switch_loading = True
        self._quick_switch_load_generation += 1
        generation = self._quick_switch_load_generation

        def run():
            try:
                from core import profile_manager

                payload = {"ok": True, "error": "", **profile_manager.get_quick_switch_summary()}
            except Exception as e:
                payload = {"ok": False, "error": str(e)}

            def finish():
                self._quick_switch_loading = False
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
                if self._quick_switch_reload_pending:
                    self._quick_switch_reload_pending = False
                    self._load_quick_switch_profiles_delayed(delay_ms=120)

            self._run_on_ui_thread(finish)

        threading.Thread(target=run, name="quick-switch-refresh", daemon=True).start()

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
        if self._exit_requested:
            return
        self._switch_preview_generation += 1
        generation = self._switch_preview_generation
        self._set_app_status(f"正在生成切换预览: {profile_name}")

        def worker():
            try:
                from core.switch_preview import build_switch_preview

                payload = {
                    "ok": True,
                    "preview": build_switch_preview(kind, profile_name),
                    "error": "",
                }
            except Exception as exc:
                payload = {"ok": False, "preview": None, "error": str(exc)}

            def finish():
                if generation != self._switch_preview_generation or self._exit_requested:
                    return
                if not payload["ok"]:
                    logger.error("Failed to build switch preview: %s", payload["error"])
                    from ui.widgets.toast import show_toast

                    show_toast(self, f"切换预览失败: {payload['error']}", is_error=True)
                    self._set_app_status("切换预览失败")
                    if on_cancel:
                        on_cancel()
                    return
                try:
                    from ui.dialogs.switch_preview_dialog import SwitchPreviewDialog

                    SwitchPreviewDialog(
                        self,
                        payload["preview"],
                        on_confirm=on_confirm,
                        on_cancel=on_cancel,
                    )
                    self._set_app_status("切换预览已打开")
                except Exception as exc:
                    logger.error("Failed to show switch preview: %s", exc, exc_info=True)
                    from ui.widgets.toast import show_toast

                    show_toast(self, f"切换预览失败: {exc}", is_error=True)
                    self._set_app_status("切换预览失败")
                    if on_cancel:
                        on_cancel()

            self._run_on_ui_thread(finish)

        threading.Thread(target=worker, name="switch-preview-build", daemon=True).start()

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
        self.after(50, self._schedule_initial_tab_load)
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

        for after_id in list(self._pending_tab_load_after_ids.values()):
            if not after_id:
                continue
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
        self._pending_tab_load_after_ids.clear()
        pending_preload_after_id = self.__dict__.get("_lazy_tab_preload_after_id")
        if pending_preload_after_id:
            try:
                self.after_cancel(pending_preload_after_id)
            except Exception:
                pass
        self._lazy_tab_preload_after_id = None
        pending_warmup_after_id = self.__dict__.get("_pending_tab_warmup_after_id")
        if pending_warmup_after_id:
            try:
                self.after_cancel(pending_warmup_after_id)
            except Exception:
                pass
        self._pending_tab_warmup_after_id = None
        tab_warmup_queue = self.__dict__.get("_tab_warmup_queue")
        if tab_warmup_queue is not None:
            tab_warmup_queue.clear()
        self._tab_class_loading.clear()
        self._tab_load_generations.clear()
        if self._quick_switch_load_after_id:
            try:
                self.after_cancel(self._quick_switch_load_after_id)
            except Exception:
                pass
            self._quick_switch_load_after_id = None
        if self._ui_callback_after_id:
            try:
                self.after_cancel(self._ui_callback_after_id)
            except Exception:
                pass
            self._ui_callback_after_id = None
        self._clear_ui_callback_queue()

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

    def _schedule_ui_callback_pump(self, delay_ms: int = 35):
        if self._exit_requested or self._ui_callback_after_id:
            return
        try:
            self._ui_callback_after_id = self.after(max(1, int(delay_ms)), self._drain_ui_callback_queue)
        except Exception as e:
            logger.debug("Failed to schedule UI callback pump: %s", e)
            self._ui_callback_after_id = None

    def _drain_ui_callback_queue(self):
        self._ui_callback_after_id = None
        callbacks = getattr(self, "_ui_callback_queue", None)
        if callbacks is None:
            return
        if self._exit_requested:
            self._clear_ui_callback_queue()
            return
        try:
            scroll_active = recent_user_scroll(self, idle_ms=UI_CALLBACK_SCROLL_IDLE_MS)
        except Exception:
            scroll_active = False
        if self._ui_callback_queue_has_pending() and scroll_active:
            self._schedule_ui_callback_pump(delay_ms=UI_CALLBACK_SCROLL_RETRY_MS)
            return
        processed = 0
        started_at = time.perf_counter()
        while processed < UI_CALLBACK_BATCH_LIMIT:
            try:
                callback = callbacks.get_nowait()
            except queue.Empty:
                break
            processed += 1
            try:
                if not self._exit_requested and self.winfo_exists():
                    callback()
            except Exception as e:
                logger.debug("UI callback failed: %s", e, exc_info=True)
            if (time.perf_counter() - started_at) * 1000 >= UI_CALLBACK_TIME_BUDGET_MS:
                break
        self._schedule_ui_callback_pump(
            delay_ms=UI_CALLBACK_BUSY_POLL_MS if self._ui_callback_queue_has_pending() else UI_CALLBACK_IDLE_POLL_MS
        )

    def _ui_callback_queue_has_pending(self) -> bool:
        callbacks = getattr(self, "_ui_callback_queue", None)
        if callbacks is None:
            return False
        try:
            return not callbacks.empty()
        except Exception:
            return False

    def _clear_ui_callback_queue(self):
        callbacks = getattr(self, "_ui_callback_queue", None)
        if callbacks is None:
            return
        while True:
            try:
                callbacks.get_nowait()
            except queue.Empty:
                return

    def _run_on_ui_thread(self, callback):
        if self._exit_requested:
            return
        if getattr(self, "_ui_thread_id", None) == threading.get_ident():
            try:
                if self.winfo_exists():
                    self.after(0, callback)
            except Exception as e:
                logger.debug("Failed to schedule UI callback: %s", e)
            return
        callbacks = getattr(self, "_ui_callback_queue", None)
        if callbacks is not None:
            callbacks.put(callback)
            return
        try:
            if self.winfo_exists():
                self.after(0, callback)
        except Exception as e:
            logger.debug("Failed to schedule UI callback: %s", e)

    def _show_env_tab(self):
        self._select_tab(ENV_TAB_LABEL)
        tab = self._loaded_tab("_env_tab")
        if tab:
            self.after(30, tab.refresh)
        else:
            self._schedule_tab_load(ENV_TAB_LABEL, delay_ms=1)
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

            dialog = ProxyQualityDialog(
                self,
                on_close=lambda: setattr(self, "_proxy_quality_dialog", None),
                on_settings_saved=self._on_proxy_quality_settings_saved,
            )
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

    def _on_proxy_quality_settings_saved(self, _settings=None):
        refresh_targets = (
            ("_local_proxy_tab", "_refresh_subscription_action_hint"),
            ("_ssh_tab", "_refresh_proxy_subscription_action_hint"),
        )
        for attr, method_name in refresh_targets:
            tab = self._loaded_tab(attr)
            if not tab:
                continue
            callback = getattr(tab, method_name, None)
            if callable(callback):
                try:
                    callback()
                except Exception as exc:
                    logger.debug("Failed to refresh proxy quality hint for %s: %s", attr, exc)
        self._set_app_status("代理质量检测设置已保存，相关页面提示已同步")

    def refresh_all(self):
        """Refresh all tabs."""
        tabs = []
        for label, attr, _module_name, _class_name, _eager in TAB_SPECS:
            tab = self._loaded_tab(attr)
            if tab and hasattr(tab, "refresh"):
                tabs.append((label, tab))
        if not tabs:
            self._status.configure(text="暂无已加载页面需要刷新")
            self._load_quick_switch_profiles()
            return

        self._status.configure(text=f"正在刷新已加载页面 0/{len(tabs)}...")

        def refresh_next(index: int = 0):
            if self._exit_requested:
                return
            if index >= len(tabs):
                self._status.configure(text="已刷新已加载页面和快捷菜单")
                if self.tray_manager.is_running():
                    self.tray_manager.update_menu()
                self._load_quick_switch_profiles()
                return
            label, tab = tabs[index]
            self._status.configure(text=f"正在刷新 {label} ({index + 1}/{len(tabs)})...")
            try:
                if tab.winfo_exists():
                    tab.refresh()
            except Exception as exc:
                logger.debug("Failed to refresh tab %s: %s", label, exc)
            self.after(45, lambda: refresh_next(index + 1))

        self.after(0, refresh_next)

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
