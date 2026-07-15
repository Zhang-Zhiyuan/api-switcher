from types import SimpleNamespace

from ui.tabs import browser_tab as browser_tab_module
from ui.tabs.browser_tab import (
    BROWSER_CLEANUP_CRITICAL_KEY,
    BrowserTab,
    _bind_browser_card_action_grid,
    _browser_diagnosis_matches_filter,
    _browser_profiles_summary,
    _diagnosis_failure,
    _visible_profile_names,
)


class _FakeButton:
    def __init__(self):
        self.state = "normal"

    def configure(self, **kwargs):
        self.state = kwargs.get("state", self.state)


class _DeferredThread:
    instances = []

    def __init__(self, *, target, **_kwargs):
        self.target = target
        self.__class__.instances.append(self)

    def start(self):
        return None


class _CriticalTop:
    def __init__(self, *, allow=True):
        self.allow = allow
        self.begun = []
        self.ended = []
        self.abandoned = []

    def _begin_critical_operation(self, key, label):
        self.begun.append((key, label))
        return self.allow

    def _end_critical_operation(self, key):
        self.ended.append(key)

    def _abandon_critical_operation(self, key):
        self.abandoned.append(key)


def _bare_browser_tab():
    tab = object.__new__(BrowserTab)
    tab._cleanup_inflight = False
    tab._bulk_buttons = [_FakeButton() for _ in range(5)]
    tab._card_cleanup_buttons = [_FakeButton()]
    tab._destroyed = False
    tab._selected_names = set()
    tab.winfo_exists = lambda: True
    tab.top = _CriticalTop()
    tab.winfo_toplevel = lambda: tab.top
    tab.toasts = []
    tab.refresh_calls = []
    tab._toast = lambda message, is_error=False: tab.toasts.append((message, is_error))
    tab.refresh = lambda: tab.refresh_calls.append(True)
    return tab


def _profile(name: str):
    return SimpleNamespace(name=name)


def test_browser_diagnosis_filter_handles_missing_keys_as_issue():
    assert _browser_diagnosis_matches_filter({}, "issues") is True
    assert _browser_diagnosis_matches_filter({}, "launchable") is False
    assert _browser_diagnosis_matches_filter({}, "resettable") is False
    assert _browser_diagnosis_matches_filter({}, "all") is True


def test_browser_profile_summary_counts_cached_diagnostics():
    profiles = [_profile("ok"), _profile("busy"), _profile("bad")]
    diagnoses = {
        "ok": {
            "valid": True,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": False,
            "can_full_reset": True,
        },
        "busy": {
            "valid": True,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": True,
            "can_full_reset": False,
        },
        "bad": {
            "valid": False,
            "executable_found": False,
            "profile_path_exists": False,
            "browser_running": False,
            "can_full_reset": False,
        },
    }

    summary = _browser_profiles_summary(profiles, diagnoses, {"ok", "missing"})

    assert summary["total_count"] == 3
    assert summary["issues_count"] == 2
    assert summary["launchable_count"] == 2
    assert summary["resettable_count"] == 1
    assert summary["selected_count"] == 1


def test_visible_profile_names_reuses_filter_without_rediagnosing():
    profiles = [_profile("ok"), _profile("bad")]
    diagnoses = {
        "ok": {
            "valid": True,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": False,
        },
        "bad": {
            "valid": False,
            "executable_found": True,
            "profile_path_exists": True,
            "browser_running": False,
        },
    }

    assert _visible_profile_names(profiles, diagnoses, "launchable") == ["ok"]
    assert _visible_profile_names(profiles, diagnoses, "issues") == ["bad"]


def test_diagnosis_failure_keeps_failed_profile_visible_as_issue():
    diagnosis = _diagnosis_failure(RuntimeError("boom"))

    assert diagnosis["valid"] is False
    assert "boom" in diagnosis["validation_error"]
    assert _browser_diagnosis_matches_filter(diagnosis, "issues") is True


def test_browser_card_action_grid_reflows_at_dpi_aware_breakpoints():
    class Container:
        def __init__(self):
            self.callback = None
            self.column_calls = []

        def winfo_width(self):
            return 1140

        def _get_widget_scaling(self):
            return 1.5

        def grid_columnconfigure(self, column, **kwargs):
            self.column_calls.append((column, kwargs))

        def bind(self, _event_name, callback, add=None):
            assert add == "+"
            self.callback = callback

    class Button:
        def __init__(self):
            self.grid_calls = []

        def grid(self, **kwargs):
            self.grid_calls.append(kwargs)

    container = Container()
    buttons = [Button() for _ in range(10)]

    _bind_browser_card_action_grid(container, buttons)
    assert [button.grid_calls[-1]["column"] for button in buttons[:7]] == [0, 1, 2, 3, 4, 5, 0]

    container.callback(SimpleNamespace(width=720))
    assert [button.grid_calls[-1]["column"] for button in buttons[:5]] == [0, 1, 0, 1, 0]

    container.callback(SimpleNamespace(width=1140))
    assert [button.grid_calls[-1]["column"] for button in buttons[:7]] == [0, 1, 2, 3, 4, 5, 0]


def test_browser_tab_suspend_cancels_initial_refresh():
    tab = object.__new__(BrowserTab)
    tab._initial_refresh_after_id = "initial"
    tab._profile_render_after_id = None
    tab._deferred_refresh_pending = False
    tab._deferred_render_pending = False
    cancelled = []
    tab.after_cancel = lambda after_id: cancelled.append(after_id)

    BrowserTab._suspend_background_work(tab)

    assert cancelled == ["initial"]
    assert tab._initial_refresh_after_id is None
    assert tab._deferred_refresh_pending is True


def test_single_site_cleanup_runs_in_background_blocks_duplicate_and_restores_controls(monkeypatch):
    tab = _bare_browser_tab()
    profile = SimpleNamespace(name="demo")
    confirmations = []
    clear_calls = []
    dispatches = []
    dispatch_widgets = []
    manager = SimpleNamespace(
        can_clear_shared_storage=lambda _profile: (True, ""),
        clear_site_data=lambda selected, scope: clear_calls.append((selected.name, scope)) or True,
    )
    monkeypatch.setattr(browser_tab_module, "browser_data_manager", manager)
    monkeypatch.setattr(
        browser_tab_module,
        "ConfirmDialog",
        lambda *_args, on_confirm, **_kwargs: confirmations.append(on_confirm),
    )
    _DeferredThread.instances = []
    monkeypatch.setattr(browser_tab_module.threading, "Thread", _DeferredThread)

    def dispatch(widget, callback, **_kwargs):
        dispatch_widgets.append(widget)
        dispatches.append(callback)
        callback()
        return True

    monkeypatch.setattr(browser_tab_module, "run_on_ui_thread", dispatch)

    tab._clear_sites(profile, "chatgpt")
    confirmations[0]()
    confirmations[0]()

    assert clear_calls == []
    assert len(_DeferredThread.instances) == 1
    assert tab._cleanup_inflight is True
    assert tab.top.begun == [(BROWSER_CLEANUP_CRITICAL_KEY, "正在清理浏览器 Profile 数据")]
    assert tab.top.ended == []
    assert all(button.state == "disabled" for button in tab._bulk_buttons[2:] + tab._card_cleanup_buttons)
    assert any("正在进行" in message for message, _is_error in tab.toasts)

    _DeferredThread.instances[0].target()

    assert clear_calls == [("demo", "chatgpt")]
    assert len(dispatches) == 1
    assert dispatch_widgets == [tab.top]
    assert tab._cleanup_inflight is False
    assert tab.top.ended == [BROWSER_CLEANUP_CRITICAL_KEY]
    assert tab.top.abandoned == []
    assert all(button.state == "normal" for button in tab._bulk_buttons[2:] + tab._card_cleanup_buttons)
    assert any("已清理 ChatGPT" in message for message, _is_error in tab.toasts)


def test_bulk_cleanup_uses_selection_snapshot_and_reports_on_ui_thread(monkeypatch):
    tab = _bare_browser_tab()
    tab._selected_names = {"one", "missing"}
    profile = SimpleNamespace(name="one")
    confirmations = []
    clear_calls = []
    results = []
    dispatches = []
    manager = SimpleNamespace(
        can_clear_shared_storage=lambda _profile: (False, "external"),
        clear_site_data=lambda selected, scope: clear_calls.append((selected.name, scope)) or False,
    )
    profile_store = SimpleNamespace(list_browser_profiles=lambda: [profile])
    monkeypatch.setattr(browser_tab_module, "browser_data_manager", manager)
    monkeypatch.setattr(browser_tab_module, "profile_manager", profile_store)
    monkeypatch.setattr(
        browser_tab_module,
        "ConfirmDialog",
        lambda *_args, on_confirm, **_kwargs: confirmations.append(on_confirm),
    )
    monkeypatch.setattr(
        browser_tab_module,
        "BulkOperationResultDialog",
        lambda *args, **kwargs: results.append((args, kwargs)),
    )
    _DeferredThread.instances = []
    monkeypatch.setattr(browser_tab_module.threading, "Thread", _DeferredThread)

    def dispatch(_widget, callback, **_kwargs):
        dispatches.append(callback)
        callback()
        return True

    monkeypatch.setattr(browser_tab_module, "run_on_ui_thread", dispatch)

    tab._bulk_clear_sites("both")
    tab._selected_names = {"changed-after-confirmation"}
    confirmations[0]()

    assert clear_calls == []
    _DeferredThread.instances[0].target()

    assert clear_calls == [("one", "both")]
    assert len(dispatches) == 1
    assert len(results) == 1
    assert results[0][1]["failure_items"] == ["missing: Profile 不存在"]
    assert tab.refresh_calls == [True]
    assert tab._cleanup_inflight is False
    assert tab.top.ended == [BROWSER_CLEANUP_CRITICAL_KEY]
    assert tab.top.abandoned == []


def test_full_reset_thread_start_failure_restores_controls_without_touching_data(monkeypatch):
    class StartFailThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    tab = _bare_browser_tab()
    profile = SimpleNamespace(name="demo")
    confirmations = []
    reset_calls = []
    manager = SimpleNamespace(full_reset=lambda selected: reset_calls.append(selected.name))
    monkeypatch.setattr(browser_tab_module, "browser_data_manager", manager)
    monkeypatch.setattr(
        browser_tab_module,
        "DangerConfirmDialog",
        lambda *_args, on_confirm, **_kwargs: confirmations.append(on_confirm),
    )
    monkeypatch.setattr(browser_tab_module.threading, "Thread", StartFailThread)

    tab._full_reset(profile)
    confirmations[0]()

    assert reset_calls == []
    assert tab._cleanup_inflight is False
    assert tab.top.begun == [(BROWSER_CLEANUP_CRITICAL_KEY, "正在清理浏览器 Profile 数据")]
    assert tab.top.ended == [BROWSER_CLEANUP_CRITICAL_KEY]
    assert tab.top.abandoned == []
    assert all(button.state == "normal" for button in tab._bulk_buttons[2:] + tab._card_cleanup_buttons)
    assert "无法启动后台清理任务" in tab.toasts[-1][0]
    assert tab.toasts[-1][1] is True


def test_cleanup_dispatch_failure_abandons_critical_operation_without_worker_tk(monkeypatch):
    tab = _bare_browser_tab()
    _DeferredThread.instances = []
    monkeypatch.setattr(browser_tab_module.threading, "Thread", _DeferredThread)
    monkeypatch.setattr(
        browser_tab_module,
        "run_on_ui_thread",
        lambda _widget, _callback, **_kwargs: False,
    )

    started = BrowserTab._start_cleanup_task(
        tab,
        lambda: "done",
        lambda _result, _error: (_ for _ in ()).throw(AssertionError("must not finish")),
        thread_name="browser-cleanup-test",
    )
    _DeferredThread.instances[0].target()

    assert started is True
    assert tab._cleanup_inflight is False
    assert tab.top.ended == []
    assert tab.top.abandoned == [BROWSER_CLEANUP_CRITICAL_KEY]


def test_cleanup_completion_releases_critical_operation_when_callback_raises(monkeypatch):
    tab = _bare_browser_tab()
    _DeferredThread.instances = []
    monkeypatch.setattr(browser_tab_module.threading, "Thread", _DeferredThread)
    monkeypatch.setattr(
        browser_tab_module,
        "run_on_ui_thread",
        lambda _widget, callback, **_kwargs: callback() is None,
    )

    BrowserTab._start_cleanup_task(
        tab,
        lambda: "done",
        lambda _result, _error: (_ for _ in ()).throw(RuntimeError("bad callback")),
        thread_name="browser-cleanup-test",
    )
    _DeferredThread.instances[0].target()

    assert tab._cleanup_inflight is False
    assert tab.top.ended == [BROWSER_CLEANUP_CRITICAL_KEY]
    assert tab.top.abandoned == []
    assert any("bad callback" in message for message, _is_error in tab.toasts)


def test_cleanup_rejected_by_app_critical_lifecycle_restores_controls(monkeypatch):
    tab = _bare_browser_tab()
    tab.top.allow = False
    monkeypatch.setattr(
        browser_tab_module.threading,
        "Thread",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("worker must not be created")),
    )

    started = BrowserTab._start_cleanup_task(
        tab,
        lambda: None,
        lambda _result, _error: None,
        thread_name="browser-cleanup-test",
    )

    assert started is False
    assert tab._cleanup_inflight is False
    assert tab.top.begun == [(BROWSER_CLEANUP_CRITICAL_KEY, "正在清理浏览器 Profile 数据")]
    assert tab.top.ended == []
    assert tab.top.abandoned == []
    assert all(button.state == "normal" for button in tab._bulk_buttons[2:] + tab._card_cleanup_buttons)


def test_browser_refresh_thread_start_failure_replaces_loading_state(monkeypatch):
    class Frame:
        def winfo_children(self):
            return []

    class Label:
        def configure(self, **_kwargs):
            pass

        def pack(self, **_kwargs):
            return self

    class StartFailThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    tab = object.__new__(BrowserTab)
    tab._initial_refresh_after_id = "initial"
    tab._destroyed = False
    tab._deferred_refresh_pending = False
    tab._cards_frame = Frame()
    tab._stats_label = Label()
    tab._refresh_generation = 0
    tab._cancel_profile_render = lambda: None
    errors = []
    tab._show_diagnostics_error = lambda message: errors.append(message)
    monkeypatch.setattr(browser_tab_module, "is_active_tab", lambda _widget: True)
    monkeypatch.setattr(browser_tab_module, "recent_user_scroll", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(browser_tab_module, "font", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(browser_tab_module.ctk, "CTkLabel", lambda *_args, **_kwargs: Label())
    monkeypatch.setattr(browser_tab_module.threading, "Thread", StartFailThread)

    BrowserTab.refresh(tab)

    assert errors == ["诊断任务启动失败: thread unavailable"]
