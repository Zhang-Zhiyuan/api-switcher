from __future__ import annotations

import sys
import threading
from types import ModuleType

from ui import app as app_module


class _StateWidget:
    def __init__(self, state="normal"):
        self.state = state

    def cget(self, name):
        assert name == "state"
        return self.state

    def configure(self, **kwargs):
        self.state = kwargs["state"]

    def set(self, value):
        self.value = value


class _Navigation:
    def __init__(self):
        self.enabled = []

    def set_enabled(self, enabled):
        self.enabled.append(enabled)


def _bare_app():
    app = object.__new__(app_module.App)
    app._critical_operation_lock = threading.RLock()
    app._critical_operations = {}
    app._critical_widget_states = []
    app._tab_navigation = _Navigation()
    app._global_action_buttons = [_StateWidget(), _StateWidget()]
    app.claude_switch = _StateWidget("readonly")
    app.codex_switch = _StateWidget("disabled")
    app.statuses = []
    app._set_app_status = lambda message: app.statuses.append(message)
    return app


def test_critical_operation_disables_navigation_and_restores_exact_widget_states():
    app = _bare_app()

    assert app_module.App._begin_critical_operation(app, "import", "正在导入") is True
    assert app._tab_navigation.enabled == [False]
    assert [button.state for button in app._global_action_buttons] == ["disabled", "disabled"]
    assert app.claude_switch.state == "disabled"
    assert app.codex_switch.state == "disabled"
    assert app_module.App._begin_critical_operation(app, "other", "其他操作") is False

    app_module.App._end_critical_operation(app, "import")

    assert app._tab_navigation.enabled == [False, True]
    assert [button.state for button in app._global_action_buttons] == ["normal", "normal"]
    assert app.claude_switch.state == "readonly"
    assert app.codex_switch.state == "disabled"


def test_exit_is_deferred_while_critical_import_is_active(monkeypatch):
    app = _bare_app()
    app._exit_requested = False
    app._critical_operations = {"portable-profile-import": "Profile 迁移包正在导入"}
    toasts = []
    monkeypatch.setattr(
        "ui.widgets.toast.show_toast",
        lambda master, message, **kwargs: toasts.append((master, message, kwargs)),
    )

    app_module.App._exit_app_now(app)

    assert app._exit_requested is False
    assert app.statuses[-1] == "Profile 迁移包正在导入，完成前不能退出"
    assert toasts == [(
        app,
        "Profile 迁移包正在导入，请等待完成后再退出",
        {"is_error": True},
    )]


def test_abandon_critical_operation_only_releases_registry_state():
    app = _bare_app()
    app._critical_operations = {"portable-profile-import": "Profile 迁移包正在导入"}

    app_module.App._abandon_critical_operation(app, "portable-profile-import")

    assert app._critical_operations == {}
    assert app._tab_navigation.enabled == []


def test_begin_critical_operation_rolls_back_registry_when_ui_state_fails():
    app = _bare_app()
    app._set_critical_operation_ui_state = lambda _busy: (_ for _ in ()).throw(RuntimeError("ui failed"))

    assert app_module.App._begin_critical_operation(app, "import", "正在导入") is False
    assert app._critical_operations == {}


def test_pending_quick_switch_refresh_stays_disabled_during_critical_import():
    app = _bare_app()
    app._critical_operations = {"portable-profile-import": "Profile 迁移包正在导入"}

    app_module.App._apply_quick_switch_profiles(
        app,
        ["Claude A"],
        "Claude A",
        ["Codex A"],
        "Codex A",
    )

    assert app.claude_switch.state == "disabled"
    assert app.codex_switch.state == "disabled"


def test_pending_switch_preview_is_cancelled_if_critical_import_starts(monkeypatch):
    build_finished = threading.Event()
    ui_callbacks = []
    dialog_calls = []
    cancel_calls = []

    switch_module = ModuleType("core.switch_preview")

    def build_switch_preview(_kind, _profile_name):
        build_finished.set()
        return object()

    switch_module.build_switch_preview = build_switch_preview
    dialog_module = ModuleType("ui.dialogs.switch_preview_dialog")
    dialog_module.SwitchPreviewDialog = lambda *args, **kwargs: dialog_calls.append((args, kwargs))
    monkeypatch.setitem(sys.modules, "core.switch_preview", switch_module)
    monkeypatch.setitem(sys.modules, "ui.dialogs.switch_preview_dialog", dialog_module)

    app = _bare_app()
    app._exit_requested = False
    app._switch_preview_generation = 0
    app._run_on_ui_thread = lambda callback: ui_callbacks.append(callback)

    app_module.App._show_switch_preview(
        app,
        "claude_api",
        "Claude A",
        on_confirm=lambda: None,
        on_cancel=lambda: cancel_calls.append(True),
    )
    assert build_finished.wait(1)
    assert len(ui_callbacks) == 1

    app._critical_operations = {"portable-profile-import": "Profile 迁移包正在导入"}
    ui_callbacks[0]()

    assert dialog_calls == []
    assert cancel_calls == [True]
    assert app.statuses[-1] == "Profile 迁移包正在导入，切换预览已取消"


def test_preview_confirmation_rechecks_critical_state_before_switch(monkeypatch):
    app = _bare_app()
    app._exit_requested = False
    app._load_quick_switch_profiles = lambda *_args, **_kwargs: None
    captured = {}
    switch_calls = []
    toasts = []
    app._show_switch_preview = lambda _kind, _name, on_confirm, _on_cancel: captured.update(
        on_confirm=on_confirm
    )
    monkeypatch.setattr(
        "core.switcher.switch_claude_profile",
        lambda profile_name: switch_calls.append(profile_name),
    )
    monkeypatch.setattr(
        "ui.widgets.toast.show_toast",
        lambda master, message, **kwargs: toasts.append((master, message, kwargs)),
    )

    app_module.App._quick_switch_claude(app, "Claude A")
    app._critical_operations = {"portable-profile-import": "Profile 迁移包正在导入"}
    captured["on_confirm"]()

    assert switch_calls == []
    assert toasts == [(
        app,
        "Profile 迁移包正在导入，暂不能切换配置",
        {"is_error": True},
    )]
    assert app.statuses[-1] == "Profile 迁移包正在导入，配置切换已取消"
