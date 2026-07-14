from types import SimpleNamespace

from ui.tabs import backup_tab as backup_tab_module
from ui.tabs.backup_tab import BackupTab, _format_size


def test_portable_import_runs_in_worker_and_finishes_on_ui_thread(monkeypatch):
    events = []
    password_callbacks = []
    dispatched = []
    toasts = []
    worker_state = {"active": False}

    class Top:
        def refresh_all(self):
            assert worker_state["active"] is False
            events.append("refresh-all")

    top = Top()

    def import_profiles(path, password):
        assert worker_state["active"] is True
        events.append(("import", path, password))
        return SimpleNamespace(
            profile_count=3,
            secret_count=2,
            browser_file_count=4,
            skipped_browser_files=["cache.bin"],
        )

    def password_dialog(master, *, on_confirm, **kwargs):
        assert master is top
        assert kwargs["confirm_password"] is False
        events.append("password-dialog")
        password_callbacks.append(on_confirm)

    class ImmediateWorkerThread:
        def __init__(self, *, target, name, daemon):
            assert name == "portable-profile-import"
            assert daemon is True
            events.append("thread-created")
            self.target = target

        def start(self):
            events.append("thread-start")
            worker_state["active"] = True
            try:
                self.target()
            finally:
                worker_state["active"] = False

    def dispatch(widget, callback):
        assert widget is top
        assert worker_state["active"] is True
        events.append("dispatch")
        dispatched.append(callback)

    def toast(master, message, **kwargs):
        assert master is top
        assert worker_state["active"] is False
        toasts.append((message, kwargs.get("is_error", False)))

    monkeypatch.setattr(
        backup_tab_module,
        "portable_migration",
        SimpleNamespace(import_portable_profiles=import_profiles),
    )
    monkeypatch.setattr(
        backup_tab_module.filedialog,
        "askopenfilename",
        lambda **kwargs: (
            events.append(("open-dialog", kwargs["parent"])),
            "selected.asxprofile",
        )[1],
    )
    monkeypatch.setattr(backup_tab_module, "PasswordDialog", password_dialog)
    monkeypatch.setattr(backup_tab_module.threading, "Thread", ImmediateWorkerThread)
    monkeypatch.setattr(backup_tab_module, "run_on_ui_thread", dispatch)
    monkeypatch.setattr(backup_tab_module, "show_toast", toast)

    top_level_calls = []
    tab = object.__new__(BackupTab)
    tab.winfo_toplevel = lambda: (top_level_calls.append(True), top)[1]

    BackupTab._import_portable(tab)

    assert events == [("open-dialog", top), "password-dialog"]
    assert top_level_calls == [True]
    assert password_callbacks

    password_callbacks[0]("strong-password")

    assert events == [
        ("open-dialog", top),
        "password-dialog",
        "thread-created",
        "thread-start",
        ("import", "selected.asxprofile", "strong-password"),
        "dispatch",
    ]
    assert tab._portable_operation_in_progress is True
    assert top_level_calls == [True]
    assert toasts == [("正在后台导入 Profile 迁移包...", False)]

    dispatched.pop()()

    assert tab._portable_operation_in_progress is False
    assert events[-1] == "refresh-all"
    assert toasts[-1] == (
        "迁移包已导入: 3 个 Profile, 2 个密钥，浏览器文件 4 个，1 个浏览器文件跳过",
        False,
    )


def test_portable_import_rejects_second_operation_until_ui_finish(monkeypatch):
    password_callbacks = []
    dispatched = []
    import_calls = []
    toasts = []
    top = object()

    def import_profiles(path, password):
        import_calls.append((path, password))
        return SimpleNamespace(
            profile_count=0,
            secret_count=0,
            browser_file_count=0,
            skipped_browser_files=[],
        )

    class ImmediateWorkerThread:
        def __init__(self, *, target, name, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(
        backup_tab_module,
        "portable_migration",
        SimpleNamespace(import_portable_profiles=import_profiles),
    )
    monkeypatch.setattr(
        backup_tab_module.filedialog,
        "askopenfilename",
        lambda **_kwargs: "selected.asxprofile",
    )
    monkeypatch.setattr(
        backup_tab_module,
        "PasswordDialog",
        lambda _master, *, on_confirm, **_kwargs: password_callbacks.append(on_confirm),
    )
    monkeypatch.setattr(backup_tab_module.threading, "Thread", ImmediateWorkerThread)
    monkeypatch.setattr(
        backup_tab_module,
        "run_on_ui_thread",
        lambda _widget, callback: dispatched.append(callback),
    )
    monkeypatch.setattr(
        backup_tab_module,
        "show_toast",
        lambda _master, message, **_kwargs: toasts.append(message),
    )

    tab = object.__new__(BackupTab)
    tab.winfo_toplevel = lambda: top
    tab.refresh = lambda: None

    BackupTab._import_portable(tab)
    password_callbacks[0]("first")
    password_callbacks[0]("second")

    assert import_calls == [("selected.asxprofile", "first")]
    assert len(dispatched) == 1
    assert toasts[-1] == "Profile 迁移包正在处理，请稍候"

    dispatched[0]()
    assert tab._portable_operation_in_progress is False


def test_portable_operation_busy_state_disables_all_conflicting_backup_actions():
    class Button:
        def __init__(self):
            self.states = []

        def configure(self, **kwargs):
            self.states.append(kwargs["state"])

    header_buttons = [Button() for _ in range(5)]
    zip_buttons = [Button() for _ in range(2)]
    backup_buttons = [Button() for _ in range(2)]
    tab = object.__new__(BackupTab)
    tab._header_action_buttons = header_buttons
    tab._zip_action_buttons = zip_buttons
    tab._backup_action_buttons = backup_buttons

    BackupTab._set_portable_operation_busy(tab, True)
    BackupTab._set_portable_operation_busy(tab, False)

    for button in header_buttons + zip_buttons + backup_buttons:
        assert button.states == ["disabled", "normal"]


def test_portable_operation_guard_blocks_conflicting_commands(monkeypatch):
    toasts = []
    tab = object.__new__(BackupTab)
    tab._portable_operation_in_progress = True
    tab.winfo_toplevel = lambda: "top"
    monkeypatch.setattr(
        backup_tab_module,
        "show_toast",
        lambda master, message, **kwargs: toasts.append((master, message, kwargs)),
    )

    assert BackupTab._portable_operation_blocked(tab) is True
    assert toasts == [(
        "top",
        "Profile 迁移包正在处理，其他备份与导入操作暂不可用",
        {"is_error": True},
    )]


def test_portable_import_registers_and_releases_app_critical_operation():
    events = []

    class Top:
        def _begin_critical_operation(self, key, label):
            events.append(("begin", key, label))
            return True

        def _end_critical_operation(self, key):
            events.append(("end", key))

    tab = object.__new__(BackupTab)
    tab._header_action_buttons = []
    tab._zip_action_buttons = []
    tab._backup_action_buttons = []
    tab._portable_operation_in_progress = False
    top = Top()

    assert BackupTab._begin_portable_operation(tab, top, critical=True) is True
    BackupTab._end_portable_operation(tab, top)

    assert events == [
        ("begin", "portable-profile-import", "Profile 迁移包正在导入"),
        ("end", "portable-profile-import"),
    ]
    assert tab._portable_operation_in_progress is False


def test_portable_abandon_releases_registry_without_touching_tk_widgets():
    abandoned = []
    top = SimpleNamespace(
        _abandon_critical_operation=lambda key: abandoned.append(key),
    )
    tab = object.__new__(BackupTab)
    tab._portable_operation_critical = True
    tab._portable_operation_in_progress = True

    BackupTab._abandon_portable_operation(tab, top)

    assert abandoned == ["portable-profile-import"]
    assert tab._portable_operation_in_progress is False
    assert tab._portable_operation_critical is False


def test_backup_size_format_is_human_readable():
    assert _format_size(999) == "999 B"
    assert _format_size(1536) == "1.5 KB"
    assert _format_size(5 * 1024 * 1024) == "5.0 MB"


def test_portable_import_progress_toast_failure_releases_critical_state(monkeypatch):
    password_callbacks = []
    ended = []

    class Top:
        def _begin_critical_operation(self, _key, _label):
            return True

        def _end_critical_operation(self, key):
            ended.append(key)

    tab = object.__new__(BackupTab)
    tab._header_action_buttons = []
    tab._zip_action_buttons = []
    tab._backup_action_buttons = []
    tab._portable_operation_in_progress = False
    top = Top()
    tab.winfo_toplevel = lambda: top

    monkeypatch.setattr(
        backup_tab_module.filedialog,
        "askopenfilename",
        lambda **_kwargs: "selected.asxprofile",
    )
    monkeypatch.setattr(
        backup_tab_module,
        "PasswordDialog",
        lambda _master, *, on_confirm, **_kwargs: password_callbacks.append(on_confirm),
    )
    monkeypatch.setattr(
        backup_tab_module,
        "show_toast",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("toast failed")),
    )

    BackupTab._import_portable(tab)
    password_callbacks[0]("strong-password")

    assert tab._portable_operation_in_progress is False
    assert ended == ["portable-profile-import"]
