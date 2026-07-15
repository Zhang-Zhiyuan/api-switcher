import threading
from types import SimpleNamespace

import pytest

from ui.tabs.session_migration_tab import (
    SESSION_MIGRATION_THREE_ACTION_COLUMNS_MIN_WIDTH,
    SESSION_MIGRATION_WIDE_MIN_WIDTH,
    SessionMigrationTab,
    _session_migration_layout,
    _session_record_summary,
)


class _ConfigRecorder:
    def __init__(self):
        self.configurations = []

    def configure(self, **kwargs):
        self.configurations.append(kwargs)


class _ControlledThread:
    instances = []

    def __init__(self, *, target, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True


class _CriticalTop:
    def __init__(self, allow=True):
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


def test_session_record_summary_counts_visible_selection_and_size():
    records = [
        SimpleNamespace(key="a", size_bytes=100),
        SimpleNamespace(key="b", size_bytes=200),
    ]

    summary = _session_record_summary(records, {"a", "missing"})

    assert summary["visible_keys"] == {"a", "b"}
    assert summary["selected_count"] == 1
    assert summary["selected_size"] == 100
    assert summary["total_size"] == 300


def test_session_record_summary_tolerates_empty_and_negative_sizes():
    records = [
        SimpleNamespace(key="a", size_bytes=None),
        SimpleNamespace(key="b", size_bytes=-10),
        SimpleNamespace(key="c", size_bytes="bad"),
    ]

    summary = _session_record_summary(records, {"a", "b"})

    assert summary["selected_count"] == 2
    assert summary["selected_size"] == 0
    assert summary["total_size"] == 0


def test_session_export_helper_preserves_explicit_empty_selection(monkeypatch):
    captured = []
    tab = object.__new__(SessionMigrationTab)
    tab._selected_keys = {"current-selection"}
    tab._provider_filter = "all"

    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.session_migration.export_sessions",
        lambda output_path, keys, content_mode: captured.append((output_path, set(keys), content_mode)) or "result",
    )

    result = SessionMigrationTab._export_current_selection_to_package(
        tab,
        "out.asxsession",
        "",
        selected_keys=set(),
        content_mode="compact",
    )

    assert result == "result"
    assert captured == [("out.asxsession", set(), "compact")]


def test_session_export_worker_uses_snapshotted_provider_filter(monkeypatch):
    captured = []
    pending_threads = []

    class ControlledThread:
        def __init__(self, *, target, name, daemon):
            assert name == "session-package-export"
            assert daemon is True
            self.target = target
            pending_threads.append(self)

        def start(self):
            pass

    tab = object.__new__(SessionMigrationTab)
    tab._selected_keys = {"claude:selected"}
    tab._provider_filter = "claude"
    tab._compact_export_var = SimpleNamespace(get=lambda: False)
    tab._stats_label = None
    tab._header_action_buttons = []
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab.winfo_toplevel = lambda: object()
    tab._current_source_ssh_name = lambda: "gpu"
    tab._run_on_ui_thread = lambda callback: True
    tab._export_current_selection_to_package = (
        lambda output_path, source, **kwargs: captured.append(
            (output_path, source, kwargs["provider_filter"])
        ) or SimpleNamespace(
            path=SimpleNamespace(stat=lambda: SimpleNamespace(st_size=1)),
            total_bytes=1,
            content_mode="full",
            session_count=1,
            file_count=1,
            omitted_output_count=0,
            skipped_keys=[],
        )
    )

    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.filedialog.asksaveasfilename",
        lambda **_kwargs: "out.asxsession",
    )
    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", ControlledThread)

    SessionMigrationTab._export_selected(tab)
    tab._provider_filter = "codex"
    pending_threads[0].target()

    assert captured == [("out.asxsession", "gpu", "claude")]


def test_session_filter_change_clears_stale_selection_before_async_refresh():
    tab = object.__new__(SessionMigrationTab)
    tab._selected_keys = {"claude:old"}
    tab._provider_filter = "claude"
    tab._visible_limit = 99
    refreshed = []
    tab.refresh = lambda: refreshed.append(True)

    SessionMigrationTab._on_filter_change(tab, "Codex CLI")

    assert tab._provider_filter == "codex"
    assert tab._selected_keys == set()
    assert tab._visible_limit == tab.MAX_VISIBLE_RECORDS
    assert refreshed == [True]


def test_session_migration_layout_breakpoints_keep_actions_reachable():
    assert _session_migration_layout(SESSION_MIGRATION_WIDE_MIN_WIDTH) == ("wide", 5)
    assert _session_migration_layout(SESSION_MIGRATION_WIDE_MIN_WIDTH - 1) == ("compact", 3)
    assert _session_migration_layout(SESSION_MIGRATION_THREE_ACTION_COLUMNS_MIN_WIDTH) == ("compact", 3)
    assert _session_migration_layout(SESSION_MIGRATION_THREE_ACTION_COLUMNS_MIN_WIDTH - 1) == ("compact", 2)


def test_session_migration_suspend_cancels_initial_refresh():
    tab = object.__new__(SessionMigrationTab)
    tab._initial_refresh_after_id = "initial"
    tab._record_render_after_id = None
    tab._deferred_refresh_pending = False
    tab._deferred_render_pending = False
    cancelled = []
    tab.after_cancel = lambda after_id: cancelled.append(after_id)
    tab._schedule_inactive_clear = lambda: None

    SessionMigrationTab._suspend_background_work(tab)

    assert cancelled == ["initial"]
    assert tab._initial_refresh_after_id is None
    assert tab._deferred_refresh_pending is True


def test_session_migration_refresh_defers_when_inactive(monkeypatch):
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._initial_refresh_after_id = "initial"
    tab._deferred_refresh_pending = False
    tab._cards_frame = object()

    monkeypatch.setattr("ui.tabs.session_migration_tab.is_active_tab", lambda _widget: False)

    SessionMigrationTab.refresh(tab)

    assert tab._initial_refresh_after_id is None
    assert tab._deferred_refresh_pending is True


def test_session_migration_refresh_thread_start_failure_replaces_loading_state(monkeypatch):
    class Frame:
        def winfo_children(self):
            return []

    class Label(_ConfigRecorder):
        def pack(self, **_kwargs):
            return self

    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    tab = object.__new__(SessionMigrationTab)
    tab._initial_refresh_after_id = "initial"
    tab._destroyed = False
    tab._deferred_refresh_pending = False
    tab._cards_frame = Frame()
    tab._stats_label = Label()
    tab._refresh_generation = 0
    tab._provider_filter = "all"
    tab._cancel_inactive_clear = lambda: None
    tab._refresh_location_options = lambda: None
    tab._cancel_record_render = lambda: None
    tab._cancel_deferred_render = lambda: None
    tab._current_source_ssh_name = lambda: ""
    tab._endpoint_label = lambda _ssh_name: "本机"
    errors = []
    tab._show_refresh_start_error = errors.append
    monkeypatch.setattr("ui.tabs.session_migration_tab.is_active_tab", lambda _widget: True)
    monkeypatch.setattr("ui.tabs.session_migration_tab.recent_user_scroll", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("ui.tabs.session_migration_tab.font", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("ui.tabs.session_migration_tab.ctk.CTkLabel", lambda *_args, **_kwargs: Label())
    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", FailingThread)

    SessionMigrationTab.refresh(tab)

    assert errors == ["thread unavailable"]


def test_session_import_runs_in_worker_and_finishes_on_ui_callback(monkeypatch):
    _ControlledThread.instances.clear()
    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", _ControlledThread)

    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = [_ConfigRecorder() for _ in range(5)]
    tab._stats_label = _ConfigRecorder()
    top = _CriticalTop()
    tab.winfo_toplevel = lambda: top
    tab.winfo_exists = lambda: True
    callbacks = []
    tab._run_on_ui_thread = lambda callback: callbacks.append(callback) or True
    imported = []
    result = SimpleNamespace(session_count=1, file_count=2)
    tab._import_package_to_endpoint = (
        lambda input_path, ssh_name, project_path: imported.append((input_path, ssh_name, project_path)) or result
    )
    shown = []
    tab._show_import_result = shown.append

    SessionMigrationTab._start_import_task(tab, "in.asxsession", "server-a", "/new/project")

    assert imported == []
    assert tab._session_operation_in_progress is True
    assert [button.configurations[-1]["state"] for button in tab._header_action_buttons[:4]] == [
        "disabled"
    ] * 4
    assert len(_ControlledThread.instances) == 1
    thread = _ControlledThread.instances[0]
    assert thread.started is True
    assert thread.name == "session-package-import"
    assert top.begun == [("session-package-import", "正在导入会话迁移包")]
    assert top.ended == []

    thread.target()

    assert imported == [("in.asxsession", "server-a", "/new/project")]
    assert shown == []
    assert len(callbacks) == 1
    assert tab._session_operation_in_progress is True

    callbacks[0]()

    assert shown == [result]
    assert tab._session_operation_in_progress is False
    assert [button.configurations[-1]["state"] for button in tab._header_action_buttons[:4]] == ["normal"] * 4
    assert top.ended == ["session-package-import"]
    assert top.abandoned == []


def test_session_import_abandons_critical_operation_when_ui_dispatch_rejects(monkeypatch):
    _ControlledThread.instances.clear()
    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", _ControlledThread)

    top = _CriticalTop()
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = []
    tab._stats_label = None
    tab.winfo_toplevel = lambda: top
    tab._run_on_ui_thread = lambda _callback: False
    tab._import_package_to_endpoint = lambda *_args: SimpleNamespace(session_count=1, file_count=1)

    SessionMigrationTab._start_import_task(tab, "in.asxsession", "")
    _ControlledThread.instances[0].target()

    assert tab._session_operation_in_progress is False
    assert top.begun == [("session-package-import", "正在导入会话迁移包")]
    assert top.ended == []
    assert top.abandoned == ["session-package-import"]


def test_session_direct_transfer_uses_critical_operation_until_ui_finish(monkeypatch):
    _ControlledThread.instances.clear()
    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", _ControlledThread)
    toasts = []
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.show_toast",
        lambda top, message, **kwargs: toasts.append((top, message, kwargs)),
    )

    top = _CriticalTop()
    callbacks = []
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = []
    tab._stats_label = None
    tab._selected_keys = {"claude:one"}
    tab._provider_filter = "claude"
    tab.winfo_toplevel = lambda: top
    tab.winfo_exists = lambda: True
    tab._run_on_ui_thread = lambda callback: callbacks.append(callback) or True
    tab._export_current_selection_to_package = lambda *_args, **_kwargs: SimpleNamespace()
    imported = SimpleNamespace(
        session_count=1,
        file_count=2,
        skipped_existing=0,
        skipped_invalid=0,
    )
    tab._import_package_to_endpoint = lambda *_args: imported
    refreshed = []
    tab.refresh = lambda: refreshed.append(True)

    SessionMigrationTab._run_transfer_task(tab, "", "server-a")

    assert top.begun == [("session-direct-transfer", "正在迁移选中会话")]
    assert len(_ControlledThread.instances) == 1
    _ControlledThread.instances[0].target()
    assert top.ended == []
    assert len(callbacks) == 1

    callbacks[0]()

    assert tab._session_operation_in_progress is False
    assert top.ended == ["session-direct-transfer"]
    assert top.abandoned == []
    assert refreshed == [True]
    assert toasts == [(top, "会话已迁移到目标: 1 个会话, 2 个文件", {})]


def test_session_direct_transfer_abandons_critical_operation_when_ui_dispatch_rejects(monkeypatch):
    _ControlledThread.instances.clear()
    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", _ControlledThread)

    top = _CriticalTop()
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = []
    tab._stats_label = None
    tab._selected_keys = {"claude:one"}
    tab._provider_filter = "claude"
    tab.winfo_toplevel = lambda: top
    tab._run_on_ui_thread = lambda _callback: False
    tab._export_current_selection_to_package = lambda *_args, **_kwargs: SimpleNamespace()
    tab._import_package_to_endpoint = lambda *_args: SimpleNamespace(session_count=1, file_count=1)

    SessionMigrationTab._run_transfer_task(tab, "", "server-a")
    tab.winfo_toplevel = lambda: (_ for _ in ()).throw(
        AssertionError("transfer worker must use the captured critical callbacks")
    )
    _ControlledThread.instances[0].target()

    assert tab._session_operation_in_progress is False
    assert top.begun == [("session-direct-transfer", "正在迁移选中会话")]
    assert top.ended == []
    assert top.abandoned == ["session-direct-transfer"]
    assert tab._session_operation_lock.acquire(blocking=False) is True
    tab._session_operation_lock.release()


@pytest.mark.parametrize(
    ("operation", "expected_key", "expected_label"),
    [
        ("import", "session-package-import", "正在导入会话迁移包"),
        ("transfer", "session-direct-transfer", "正在迁移选中会话"),
    ],
)
def test_session_critical_rejection_does_not_start_worker_or_hold_local_lock(
    monkeypatch,
    operation,
    expected_key,
    expected_label,
):
    class UnexpectedThread:
        def __init__(self, **_kwargs):
            raise AssertionError("critical rejection must not create a worker")

    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", UnexpectedThread)
    monkeypatch.setattr("ui.tabs.session_migration_tab.show_toast", lambda *_args, **_kwargs: None)

    top = _CriticalTop(allow=False)
    tab = object.__new__(SessionMigrationTab)
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = []
    tab._stats_label = None
    tab._selected_keys = {"claude:one"}
    tab._provider_filter = "claude"
    tab.winfo_toplevel = lambda: top

    if operation == "import":
        SessionMigrationTab._start_import_task(tab, "in.asxsession", "")
    else:
        SessionMigrationTab._run_transfer_task(tab, "", "server-a")

    assert tab._session_operation_in_progress is False
    assert top.begun == [(expected_key, expected_label)]
    assert top.ended == []
    assert top.abandoned == []
    assert tab._session_operation_lock.acquire(blocking=False) is True
    tab._session_operation_lock.release()


def test_session_critical_rejection_tolerates_toast_failure_and_releases_lock(monkeypatch):
    top = _CriticalTop(allow=False)
    tab = object.__new__(SessionMigrationTab)
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = []
    tab._stats_label = None
    tab.winfo_toplevel = lambda: top
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.show_toast",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("toast unavailable")),
    )

    SessionMigrationTab._start_import_task(tab, "in.asxsession", "")

    assert tab._session_operation_in_progress is False
    assert tab._session_operation_lock.acquire(blocking=False) is True
    tab._session_operation_lock.release()


def test_session_operation_mutex_rejects_second_task(monkeypatch):
    tab = object.__new__(SessionMigrationTab)
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = [_ConfigRecorder() for _ in range(5)]
    tab.winfo_toplevel = lambda: "top"
    toasts = []
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.show_toast",
        lambda top, message, **kwargs: toasts.append((top, message, kwargs)),
    )

    assert SessionMigrationTab._begin_session_operation(tab) is True
    assert SessionMigrationTab._begin_session_operation(tab) is False

    assert tab._session_operation_in_progress is True
    assert toasts == [("top", "会话迁移任务正在处理，请稍候", {"is_error": True})]
    SessionMigrationTab._end_session_operation(tab)
    assert tab._session_operation_in_progress is False


def test_busy_session_operation_prevents_import_export_and_transfer_threads(monkeypatch):
    class UnexpectedThread:
        def __init__(self, **_kwargs):
            raise AssertionError("a second worker must not be created")

    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", UnexpectedThread)
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.filedialog.asksaveasfilename",
        lambda **_kwargs: "out.asxsession",
    )
    toasts = []
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.show_toast",
        lambda _top, message, **_kwargs: toasts.append(message),
    )

    tab = object.__new__(SessionMigrationTab)
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = [_ConfigRecorder() for _ in range(5)]
    tab._selected_keys = {"selected"}
    tab._provider_filter = "all"
    tab._stats_label = None
    tab._compact_export_var = SimpleNamespace(get=lambda: False)
    tab.winfo_toplevel = lambda: "top"
    tab._current_source_ssh_name = lambda: ""

    assert SessionMigrationTab._begin_session_operation(tab) is True

    SessionMigrationTab._start_import_task(tab, "in.asxsession", "")
    SessionMigrationTab._export_selected(tab)
    SessionMigrationTab._run_transfer_task(tab, "", "server-a")

    assert toasts == ["会话迁移任务正在处理，请稍候"] * 3
    SessionMigrationTab._end_session_operation(tab)


def test_session_import_thread_start_failure_restores_operation_state(monkeypatch):
    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", FailingThread)
    toasts = []
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.show_toast",
        lambda top, message, **kwargs: toasts.append((top, message, kwargs)),
    )

    tab = object.__new__(SessionMigrationTab)
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = [_ConfigRecorder() for _ in range(5)]
    tab._stats_label = _ConfigRecorder()
    top = _CriticalTop()
    tab.winfo_toplevel = lambda: top
    stats_updates = []
    tab._update_stats_label = lambda: stats_updates.append(True)

    SessionMigrationTab._start_import_task(tab, "in.asxsession", "")

    assert tab._session_operation_in_progress is False
    assert stats_updates == [True]
    assert [button.configurations[-1]["state"] for button in tab._header_action_buttons[:4]] == ["normal"] * 4
    assert toasts == [
        (
            top,
            "无法启动导入任务: thread unavailable",
            {"is_error": True},
        )
    ]
    assert top.begun == [("session-package-import", "正在导入会话迁移包")]
    assert top.ended == ["session-package-import"]
    assert top.abandoned == []


def test_session_transfer_thread_start_failure_restores_critical_and_local_state(monkeypatch):
    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr("ui.tabs.session_migration_tab.threading.Thread", FailingThread)
    toasts = []
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.show_toast",
        lambda top, message, **kwargs: toasts.append((top, message, kwargs)),
    )

    top = _CriticalTop()
    tab = object.__new__(SessionMigrationTab)
    tab._session_operation_lock = threading.Lock()
    tab._session_operation_in_progress = False
    tab._header_action_buttons = [_ConfigRecorder() for _ in range(5)]
    tab._stats_label = _ConfigRecorder()
    tab._selected_keys = {"claude:one"}
    tab._provider_filter = "claude"
    tab.winfo_toplevel = lambda: top
    stats_updates = []
    tab._update_stats_label = lambda: stats_updates.append(True)

    SessionMigrationTab._run_transfer_task(tab, "", "server-a")

    assert tab._session_operation_in_progress is False
    assert stats_updates == [True]
    assert [button.configurations[-1]["state"] for button in tab._header_action_buttons[:4]] == ["normal"] * 4
    assert top.begun == [("session-direct-transfer", "正在迁移选中会话")]
    assert top.ended == ["session-direct-transfer"]
    assert top.abandoned == []
    assert toasts == [
        (
            top,
            "无法启动迁移任务: thread unavailable",
            {"is_error": True},
        )
    ]
