from __future__ import annotations

import io
import queue
import sys
import time
import threading
from types import ModuleType, SimpleNamespace

import main
from ui import app as app_module
from ui.startup_splash import (
    SPLASH_ARG,
    StartupSplash,
    _iter_stdin_lines_utf8,
    _splash_subprocess_env,
    splash_process_supported,
)


def test_parse_args_defaults_to_splash_enabled():
    args = main.parse_args([])

    assert args.start_minimized is False
    assert args.no_splash is False


def test_quick_switch_labels_identify_target_tools():
    assert app_module.PROXY_QUALITY_DIALOG_LABEL == "代理质量检测"
    assert app_module.QUICK_SWITCH_TITLE == "快速切换 API"
    assert app_module.CLAUDE_QUICK_SWITCH_LABEL == "Claude Code 使用"
    assert app_module.CODEX_QUICK_SWITCH_LABEL == "Codex CLI 使用"


def test_proxy_quality_is_not_a_primary_tab():
    labels = [label for label, *_spec in app_module.TAB_SPECS]

    assert "环境检测" not in labels
    assert "环境监测" not in labels
    assert app_module.PROXY_QUALITY_DIALOG_LABEL not in labels
    assert hasattr(app_module.App, "_show_proxy_quality_dialog")
    assert hasattr(app_module.App, "_on_proxy_quality_settings_saved")
    assert not hasattr(app_module.App, "_show_network_diagnostics_tab")


def test_primary_tabs_are_lazy_loaded_and_priority_preloaded_after_startup():
    specs = {label: eager for label, _attr, _module_name, _class_name, eager in app_module.TAB_SPECS}

    assert app_module.DEFAULT_TAB_PRELOAD_MODE == "priority"
    assert app_module.DEFAULT_TAB_WARMUP_MODE == "0"
    assert app_module.TAB_CLASS_PRELOAD_START_MS >= 3000
    assert app_module.QUICK_SWITCH_INITIAL_LOAD_MS >= 2000
    assert app_module.TAB_WARMUP_INTERACTION_IDLE_MS >= 1500
    assert specs["Claude Code"] is False
    assert specs["Codex CLI"] is False
    assert all(eager is False for eager in specs.values())
    assert hasattr(app_module.App, "_load_quick_switch_profiles_delayed")
    assert hasattr(app_module.App, "_run_quick_switch_profile_load")
    assert hasattr(app_module.App, "_schedule_lazy_tab_preload")
    assert hasattr(app_module.App, "_start_lazy_tab_warmup")
    assert hasattr(app_module.App, "_warm_next_lazy_tab")


def test_lazy_tab_preload_waits_for_user_idle(monkeypatch):
    after_callbacks = []
    preload_calls = []

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._lazy_tab_preload_started = False
    app._lazy_tab_preload_after_id = None
    app._recent_user_interaction = lambda idle_ms=0: True
    app._ui_callback_queue_has_pending = lambda: False
    app._preload_lazy_tab_classes = lambda priority_only=False: preload_calls.append(priority_only)
    app.after = lambda delay, callback: after_callbacks.append((delay, callback)) or f"after-{len(after_callbacks)}"

    monkeypatch.setattr(app_module, "recent_user_scroll", lambda *_args, **_kwargs: False)

    app_module.App._schedule_lazy_tab_preload(app, "priority", delay_ms=5)
    assert after_callbacks[0][0] == 5

    after_callbacks[0][1]()

    assert preload_calls == []
    assert after_callbacks[1][0] == app_module.TAB_CLASS_PRELOAD_RETRY_MS


def test_lazy_tab_warmup_prioritizes_heavy_tabs_after_current_tab():
    class FakeTabView:
        def get(self):
            return "Claude Code"

    scheduled = []
    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._lazy_tab_warmup_started = False
    app._tab_warmup_queue = []
    app._tabview = FakeTabView()
    app._schedule_next_tab_warmup = lambda delay_ms=0: scheduled.append(delay_ms)

    app_module.App._start_lazy_tab_warmup(app, priority_only=True)

    assert app._tab_warmup_queue[:2] == ["Win11 代理", "SSH 服务器"]
    assert "Claude Code" not in app._tab_warmup_queue
    assert "Codex CLI" in app._tab_warmup_queue
    assert scheduled == [0]


def test_lazy_tab_warmup_defers_when_ui_queue_is_busy(monkeypatch):
    scheduled = []
    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._ui_callback_queue_has_pending = lambda: True
    app._schedule_next_tab_warmup = lambda delay_ms=0: scheduled.append(delay_ms)

    monkeypatch.setattr(app_module, "recent_user_scroll", lambda *_args, **_kwargs: False)

    app_module.App._warm_next_lazy_tab(app)

    assert scheduled == [app_module.TAB_WARMUP_RETRY_MS]


def test_lazy_tab_warmup_defers_after_recent_user_interaction(monkeypatch):
    scheduled = []
    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._last_user_interaction_at = time.perf_counter()
    app._ui_callback_queue_has_pending = lambda: False
    app._schedule_next_tab_warmup = lambda delay_ms=0: scheduled.append(delay_ms)

    monkeypatch.setattr(app_module, "recent_user_scroll", lambda *_args, **_kwargs: False)

    app_module.App._warm_next_lazy_tab(app)

    assert scheduled == [app_module.TAB_WARMUP_RETRY_MS]


def test_tray_startup_runs_off_ui_thread():
    class SlowTray:
        def __init__(self):
            self.available_entered = threading.Event()
            self.start_called = threading.Event()

        def is_running(self):
            return False

        def is_available(self):
            self.available_entered.set()
            time.sleep(0.15)
            return True

        def start(self):
            self.start_called.set()

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._tray_starting = False
    app.tray_manager = SlowTray()

    started_at = time.perf_counter()
    app_module.App._start_tray_icon(app)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.05
    assert app.tray_manager.available_entered.wait(1)
    assert app.tray_manager.start_called.wait(1)


def test_tray_startup_ignores_duplicate_start_while_pending():
    class BlockingTray:
        def __init__(self):
            self.release = threading.Event()
            self.available_entered = threading.Event()
            self.available_calls = 0
            self.start_calls = 0

        def is_running(self):
            return False

        def is_available(self):
            self.available_calls += 1
            self.available_entered.set()
            self.release.wait(1)
            return True

        def start(self):
            self.start_calls += 1

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._tray_starting = False
    app.tray_manager = BlockingTray()

    app_module.App._start_tray_icon(app)
    assert app.tray_manager.available_entered.wait(1)
    app_module.App._start_tray_icon(app)
    app.tray_manager.release.set()

    deadline = time.time() + 1
    while app._tray_starting and time.time() < deadline:
        time.sleep(0.01)

    assert app.tray_manager.available_calls == 1
    assert app.tray_manager.start_calls == 1


def test_lazy_tray_manager_status_check_does_not_load_tray_core():
    sys.modules.pop("core.tray_manager", None)

    manager = app_module._LazyTrayManager(
        on_show_window=lambda: None,
        on_exit=lambda: None,
        on_startup_changed=lambda: None,
        on_hide_window=lambda: None,
    )

    assert manager.is_running() is False
    assert "core.tray_manager" not in sys.modules


def test_tray_profile_change_refreshes_main_window_state():
    refreshed = []
    quick_switch_delays = []
    status_updates = []

    app = object.__new__(app_module.App)
    app._run_on_ui_thread = lambda callback: callback()
    app._refresh_loaded_tab = lambda attr: refreshed.append(attr)
    app._load_quick_switch_profiles = lambda delay_ms=80: quick_switch_delays.append(delay_ms)
    app._status = SimpleNamespace(configure=lambda **kwargs: status_updates.append(kwargs))

    app_module.App._on_profile_changed_from_tray(app, "claude", "relay")

    assert refreshed == ["_claude_tab", "_usage_stats_tab"]
    assert quick_switch_delays == [0]
    assert status_updates == [{"text": "已从托盘切换 Claude API 配置: relay"}]


def test_run_on_ui_thread_queues_worker_callbacks_until_ui_pump():
    callbacks = []
    after_calls = []

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._ui_thread_id = threading.get_ident()
    app._ui_callback_queue = queue.Queue()
    app._ui_callback_after_id = None
    app.winfo_exists = lambda: True
    app.after = lambda delay, callback: after_calls.append((delay, callback)) or "after-id"

    worker_finished = threading.Event()

    def worker():
        app_module.App._run_on_ui_thread(app, lambda: callbacks.append("done"))
        worker_finished.set()

    threading.Thread(target=worker, daemon=True).start()

    assert worker_finished.wait(1)
    assert callbacks == []
    assert after_calls == []

    app_module.App._drain_ui_callback_queue(app)

    assert callbacks == ["done"]
    assert len(after_calls) == 1
    assert after_calls[0][0] == app_module.UI_CALLBACK_IDLE_POLL_MS
    assert after_calls[0][1].__self__ is app
    assert after_calls[0][1].__func__ is app_module.App._drain_ui_callback_queue


def test_ui_callback_pump_uses_batch_limit_for_backlog():
    callbacks = []
    after_calls = []

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._ui_callback_queue = queue.Queue()
    app._ui_callback_after_id = None
    app.winfo_exists = lambda: True
    app.after = lambda delay, callback: after_calls.append((delay, callback)) or "after-id"

    for index in range(app_module.UI_CALLBACK_BATCH_LIMIT + 3):
        app._ui_callback_queue.put(lambda index=index: callbacks.append(index))

    app_module.App._drain_ui_callback_queue(app)

    assert len(callbacks) == app_module.UI_CALLBACK_BATCH_LIMIT
    assert after_calls[0][0] == app_module.UI_CALLBACK_BUSY_POLL_MS


def test_ui_callback_pump_defers_during_recent_scroll(monkeypatch):
    callbacks = []
    after_calls = []

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._ui_callback_queue = queue.Queue()
    app._ui_callback_after_id = None
    app.winfo_exists = lambda: True
    app.after = lambda delay, callback: after_calls.append((delay, callback)) or "after-id"
    app._ui_callback_queue.put(lambda: callbacks.append("ran"))

    monkeypatch.setattr(app_module, "recent_user_scroll", lambda *_args, **_kwargs: True)

    app_module.App._drain_ui_callback_queue(app)

    assert callbacks == []
    assert after_calls[0][0] == app_module.UI_CALLBACK_SCROLL_RETRY_MS
    assert app._ui_callback_queue.qsize() == 1


def test_background_work_targets_use_tab_declared_targets():
    class Target:
        def __init__(self, alive=True):
            self.alive = alive

        def winfo_exists(self):
            return self.alive

    target = Target()
    duplicate = target
    dead = Target(alive=False)

    class Tab(Target):
        def _iter_background_work_targets(self):
            return [self, target, duplicate, dead, None]

    app = object.__new__(app_module.App)
    tab = Tab()

    targets = list(app_module.App._iter_background_work_targets(app, tab))

    assert targets == [tab, target]


def test_background_work_targets_fall_back_to_tab_itself():
    class Tab:
        pass

    app = object.__new__(app_module.App)
    tab = Tab()

    assert list(app_module.App._iter_background_work_targets(app, tab)) == [tab]


def test_claude_and_codex_defer_auto_continue_control_until_active(monkeypatch):
    from ui.tabs import claude_tab, codex_tab

    for module, tab_class in ((claude_tab, claude_tab.ClaudeTab), (codex_tab, codex_tab.CodexTab)):
        tab = object.__new__(tab_class)
        tab._auto_continue_after_id = None
        tab._auto_continue_control = None
        tab._auto_continue_host = object()
        tab._deferred_auto_continue_pending = False
        tab._destroyed = False

        monkeypatch.setattr(module, "is_active_tab", lambda _tab: False)

        tab_class._build_auto_continue_control(tab)

        assert tab._auto_continue_after_id is None
        assert tab._deferred_auto_continue_pending is True


def test_claude_and_codex_resume_deferred_auto_continue_control():
    from ui.tabs import claude_tab, codex_tab

    for module, tab_class in ((claude_tab, claude_tab.ClaudeTab), (codex_tab, codex_tab.CodexTab)):
        calls = []
        tab = object.__new__(tab_class)
        tab._auto_continue_after_id = None
        tab._auto_continue_control = None
        tab._auto_continue_host = object()
        tab._deferred_auto_continue_pending = True
        tab._profile_render_after_id = None
        tab._profile_render_after_ids = set()
        tab._deferred_render_pending = False
        tab._destroyed = False
        tab.after = lambda delay, callback: calls.append((delay, callback)) or "after-id"

        tab_class._resume_background_work(tab)

        assert calls and calls[0][0] == module.DEFERRED_CONTROL_RETRY_MS
        assert tab._auto_continue_after_id == "after-id"
        assert tab._deferred_auto_continue_pending is False


def test_env_tab_defers_remote_env_control_until_active(monkeypatch):
    from ui.tabs import env_tab

    tab = object.__new__(env_tab.EnvTab)
    tab._remote_env_after_id = None
    tab._remote_env_control = None
    tab._remote_env_host = object()
    tab._deferred_remote_env_pending = False
    tab._destroyed = False

    monkeypatch.setattr(env_tab, "is_active_tab", lambda _tab: False)

    env_tab.EnvTab._build_remote_env_control(tab)

    assert tab._remote_env_after_id is None
    assert tab._deferred_remote_env_pending is True


def test_env_tab_resumes_deferred_remote_env_control():
    from ui.tabs import env_tab

    calls = []
    tab = object.__new__(env_tab.EnvTab)
    tab._remote_env_after_id = None
    tab._remote_env_control = None
    tab._remote_env_host = object()
    tab._deferred_remote_env_pending = True
    tab._destroyed = False
    tab.after = lambda delay, callback: calls.append((delay, callback)) or "after-id"

    env_tab.EnvTab._resume_background_work(tab)

    assert calls and calls[0][0] == env_tab.REMOTE_ENV_BUILD_RETRY_MS
    assert tab._remote_env_after_id == "after-id"
    assert tab._deferred_remote_env_pending is False


def test_env_tab_suspend_tolerates_partial_initialization():
    from ui.tabs import env_tab

    cancelled = []
    tab = object.__new__(env_tab.EnvTab)
    tab._remote_env_after_id = "remote-after"
    tab._deferred_remote_env_pending = False
    tab.after_cancel = lambda after_id: cancelled.append(after_id)

    env_tab.EnvTab._suspend_background_work(tab)

    assert cancelled == ["remote-after"]
    assert tab._remote_env_after_id is None
    assert tab._deferred_remote_env_pending is True


def test_tab_change_schedules_load_without_extra_delay():
    calls = []

    class TabView:
        def get(self):
            return "Win11 代理"

    app = object.__new__(app_module.App)
    app._tabview = TabView()
    app._suspend_inactive_tab_work = lambda label: calls.append(("suspend", label))
    app._schedule_tab_load = lambda label, delay_ms=25: calls.append(("load", label, delay_ms))
    app._resume_active_tab_work = lambda label: calls.append(("resume", label))

    app_module.App._on_tab_changed(app)

    assert calls == [
        ("suspend", "Win11 代理"),
        ("load", "Win11 代理", 1),
        ("resume", "Win11 代理"),
    ]


def test_tab_change_refreshes_footer_for_an_already_loaded_tab():
    messages = []

    class TabView:
        def get(self):
            return "日志查看器"

    app = object.__new__(app_module.App)
    app._tabview = TabView()
    app._tab_specs = {"日志查看器": ("_log_viewer_tab", "module", "Class", False)}
    app._tab_navigation = None
    app._log_viewer_tab = object()
    app._tab_is_loaded = lambda attr: attr == "_log_viewer_tab"
    app._set_app_status = messages.append
    app._suspend_inactive_tab_work = lambda _label: None
    app._schedule_tab_load = lambda _label, delay_ms=25: None
    app._resume_active_tab_work = lambda _label: None

    app_module.App._on_tab_changed(app)

    assert messages == ["已加载 日志查看器"]


def test_shutdown_clears_pending_ui_callbacks(monkeypatch):
    import core

    local_proxy_module = ModuleType("core.local_proxy")
    local_proxy_module.local_proxy_keep_running_on_exit_enabled = lambda: True
    ssh_module = ModuleType("core.ssh_manager")

    class FakeSSHManager:
        def disconnect_all(self):
            pass

    ssh_module.ssh_manager = FakeSSHManager()
    monkeypatch.setitem(sys.modules, "core.local_proxy", local_proxy_module)
    monkeypatch.setitem(sys.modules, "core.ssh_manager", ssh_module)
    monkeypatch.setattr(core, "local_proxy", local_proxy_module, raising=False)

    app = object.__new__(app_module.App)
    app._exit_requested = True
    app._close_dialog = None
    app._pending_tab_load_after_ids = {}
    app._tab_class_loading = set()
    app._tab_load_generations = {}
    app._quick_switch_load_after_id = None
    app._ui_callback_after_id = "after-id"
    app._ui_callback_queue = queue.Queue()
    app._ui_callback_queue.put(lambda: None)
    app._proxy_quality_dialog = None
    app.tray_manager = type("Tray", (), {"stop": lambda self: None})()
    app.after_cancel = lambda after_id: callbacks.append(after_id)
    app.winfo_exists = lambda: True
    callbacks = []
    for _label, attr, _module_name, _class_name, _eager in app_module.TAB_SPECS:
        setattr(app, attr, None)

    app_module.App._shutdown_runtime_resources(app)

    assert callbacks == ["after-id"]
    assert app._ui_callback_after_id is None
    assert app._ui_callback_queue.empty()


def test_switch_preview_build_runs_off_ui_thread(monkeypatch):
    build_started = threading.Event()
    dialog_created = threading.Event()
    preview = object()
    statuses = []
    captured = {}

    switch_module = ModuleType("core.switch_preview")

    def build_switch_preview(kind, name):
        build_started.set()
        time.sleep(0.15)
        captured["build"] = (kind, name)
        return preview

    switch_module.build_switch_preview = build_switch_preview

    dialog_module = ModuleType("ui.dialogs.switch_preview_dialog")

    class FakeSwitchPreviewDialog:
        def __init__(self, master, preview_arg, on_confirm=None, on_cancel=None):
            captured["dialog"] = (master, preview_arg, on_confirm, on_cancel)
            dialog_created.set()

    dialog_module.SwitchPreviewDialog = FakeSwitchPreviewDialog

    monkeypatch.setitem(sys.modules, "core.switch_preview", switch_module)
    monkeypatch.setitem(sys.modules, "ui.dialogs.switch_preview_dialog", dialog_module)

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._switch_preview_generation = 0
    app._set_app_status = lambda message: statuses.append(message)
    app._run_on_ui_thread = lambda callback: callback()

    started_at = time.perf_counter()
    app_module.App._show_switch_preview(app, "claude_api", "fast-profile", on_confirm=lambda: None)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.05
    assert build_started.wait(1)
    assert dialog_created.wait(1)
    assert captured["build"] == ("claude_api", "fast-profile")
    assert captured["dialog"][0] is app
    assert captured["dialog"][1] is preview
    assert statuses[0] == "正在生成切换预览: fast-profile"
    assert statuses[-1] == "切换预览已打开"


def test_lazy_tab_class_load_runs_off_ui_thread(monkeypatch):
    import_started = threading.Event()
    tab_created = threading.Event()
    captured = {}

    slow_module = ModuleType("slow_tab_module")

    class FakeFrame:
        def winfo_children(self):
            return []

    class SlowTab:
        def __init__(self, master):
            captured["master"] = master
            tab_created.set()

        def pack(self, **kwargs):
            captured["pack"] = kwargs

        def winfo_exists(self):
            return True

    slow_module.SlowTab = SlowTab
    real_import_module = app_module.importlib.import_module

    def slow_import_module(name):
        if name == "slow_tab_module":
            import_started.set()
            time.sleep(0.15)
            return slow_module
        return real_import_module(name)

    monkeypatch.setattr(app_module.importlib, "import_module", slow_import_module)

    app = object.__new__(app_module.App)
    app._exit_requested = False
    app._pending_tab_load_after_ids = {}
    app._tab_class_loading = set()
    app._tab_load_generations = {}
    app._tab_class_cache = {}
    app._tab_class_cache_lock = threading.RLock()
    app._tab_specs = {"Slow": ("_slow_tab", "slow_tab_module", "SlowTab", False)}
    app._tab_frames = {"Slow": FakeFrame()}
    app._slow_tab = None
    app._show_tab_loading = lambda _label: None
    app._show_tab_error = lambda _label, error: (_ for _ in ()).throw(error)
    app._set_app_status = lambda message: captured.setdefault("statuses", []).append(message)
    app._run_on_ui_thread = lambda callback: callback()

    def fake_after(_delay_ms, callback):
        threading.Timer(0, callback).start()
        return "after-id"

    app.after = fake_after

    started_at = time.perf_counter()
    app_module.App._schedule_tab_load(app, "Slow", delay_ms=1)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.05
    assert import_started.wait(1)
    assert tab_created.wait(1)
    assert app._slow_tab is not None
    assert captured["master"] is app._tab_frames["Slow"]
    assert captured["pack"] == {"fill": "both", "expand": True}


def test_ssh_heavy_sections_are_delayed():
    from ui.tabs.ssh_tab import SSHTab

    assert hasattr(SSHTab, "_build_deployment_sections")
    assert hasattr(SSHTab, "_install_remote_auto_section_placeholder")
    assert hasattr(SSHTab, "_build_remote_auto_section")


def test_proxy_quality_dialog_module_is_importable():
    from ui.dialogs.proxy_quality_dialog import ProxyQualityDialog
    from ui.widgets.proxy_quality_panel import ProxyQualityPanel

    assert ProxyQualityDialog.__name__ == "ProxyQualityDialog"
    assert ProxyQualityPanel.__name__ == "ProxyQualityPanel"


def test_parse_args_supports_no_splash_and_minimized_aliases():
    args = main.parse_args(["--tray", "--no-splash", "--ignored"])

    assert args.start_minimized is True
    assert args.no_splash is True
    assert args.splash_child is False


def test_parse_args_supports_hidden_splash_child_mode():
    args = main.parse_args([SPLASH_ARG])

    assert args.splash_child is True


def test_disabled_startup_splash_is_noop():
    splash = StartupSplash(enabled=False)

    assert splash.visible is False
    splash.pulse("ignored")
    splash.keep_visible_for(0)
    splash.close()
    assert splash.visible is False


def test_startup_splash_is_disabled_for_frozen_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    splash = StartupSplash()

    assert splash_process_supported() is False
    assert splash.visible is False
    splash.close()


def test_startup_splash_reads_status_pipe_as_utf8():
    stdin = ModuleType("stdin")
    stdin.buffer = io.BytesIO("STATUS\t正在准备配置...\nCLOSE\n".encode("utf-8"))

    assert list(_iter_stdin_lines_utf8(stdin)) == ["STATUS\t正在准备配置...", "CLOSE"]


def test_startup_splash_child_forces_utf8_environment():
    env = _splash_subprocess_env()

    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_flush_usage_session_ends_active_recorder(monkeypatch):
    calls = []
    module = ModuleType("core.usage_recorder")

    class FakeUsageRecorder:
        def end_session(self):
            calls.append("ended")

    module.usage_recorder = FakeUsageRecorder()
    monkeypatch.setitem(sys.modules, "core.usage_recorder", module)

    main.flush_usage_session()

    assert calls == ["ended"]
