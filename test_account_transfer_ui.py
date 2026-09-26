"""Isolated UI contracts for per-account login transfer; no real auth files."""

from queue import Queue
from types import SimpleNamespace

import pytest

from ui.dialogs import account_transfer_dialog as module
from ui.tabs import claude_tab, codex_tab


class Widget:
    def __init__(self, value=""):
        self.value = value
        self.options = {}

    def get(self):
        return self.value

    def configure(self, **options):
        self.options.update(options)

    def cget(self, option):
        return self.options.get(option, "")

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value = value


@pytest.fixture
def dialog(monkeypatch):
    instance = object.__new__(module.AccountTransferDialog)
    events, pending, callbacks = [], [], []
    instance._owner = SimpleNamespace(
        _begin_critical_operation=lambda key, _label: events.append(("begin", key)) or True,
        _end_critical_operation=lambda key: events.append(("end", key)),
        _abandon_critical_operation=lambda key: events.append(("abandon", key)),
    )
    instance._busy = False
    instance._destroyed = False
    instance._result = None
    instance._critical_key = "test-transfer"
    instance._critical_owned = False
    instance._messages = Queue()
    instance._poll_after_id = None
    instance._poll_failed = False
    instance._export_encrypted = False
    instance._password_trace = None
    instance._profile_type = "codex"
    instance._exporting = False
    instance._account_name = None
    instance._title = "导入 Codex 登录包"
    instance._path = Widget("selected.asxaccount")
    instance._password = Widget("transfer-password")
    instance._confirmation = None
    instance._primary = Widget()
    instance._cancel = Widget()
    instance._browse = Widget()
    instance._status = Widget()
    instance._inputs = [instance._path, instance._password, instance._browse]
    instance._on_imported = lambda: events.append("refresh")
    instance._on_activate = lambda name: events.append(("activate", name))
    instance.after = lambda _delay, callback: callbacks.append(callback) or "timer"
    instance.after_idle = lambda callback: callbacks.append(callback) or "idle"
    instance.after_cancel = lambda token: events.append(("cancel-timer", token))
    instance.destroy = lambda: events.append("destroy")

    class Thread:
        def __init__(self, *, target, name, daemon):
            assert name == "account-login-transfer"
            assert daemon is True
            self.target = target

        def start(self):
            pending.append(self.target)

    monkeypatch.setattr(module.threading, "Thread", Thread)
    monkeypatch.setattr(module, "run_on_ui_thread", lambda _widget, cb: callbacks.append(cb) or True)
    result = SimpleNamespace(profile_type="codex", account_name="Imported account", created_new=True)
    calls = []
    monkeypatch.setattr(module, "account_transfer", SimpleNamespace(
        import_account_login=lambda *args, **kwargs: calls.append(("import", args, kwargs)) or result,
        export_account_login=lambda *args, **kwargs: calls.append(("export", args, kwargs)) or result,
    ))
    return instance, events, pending, callbacks, calls


def test_import_background_default_keeps_login_until_explicit_activation(dialog):
    item, events, pending, _callbacks, calls = dialog
    item._submit()
    assert item._busy
    assert calls == []
    item._submit()
    assert len(pending) == 1
    pending.pop()()
    assert calls == [("import", ("selected.asxaccount", "transfer-password"), {"expected_type": "codex"})]
    assert "refresh" not in events
    item._poll_result()
    assert not item._busy
    assert events.count(("end", "test-transfer")) == 1
    assert "refresh" in events
    assert not any(isinstance(event, tuple) and event[0] == "activate" for event in events)
    assert item._primary.options["text"] == "切换到此账号"
    assert item._password.get() == ""
    item._primary.options["command"]()
    assert events[-2:] == ["destroy", ("activate", "Imported account")]


def test_import_shows_stale_bundle_warning_without_claiming_login_success(dialog):
    item, _events, _pending, _callbacks, _calls = dialog
    item._finish(SimpleNamespace(account_name="Saved account", warnings=("已保留本机较新凭据",)), None)
    text = item._status.options["text"]
    assert "已保留本机较新凭据" in text
    assert "未验证服务端登录" in text
    assert "refresh token" in text
    assert "登录成功" not in text


@pytest.mark.parametrize("name", [None, "Saved official account"])
def test_export_current_or_named_account_no_activation(dialog, name):
    item, events, pending, _callbacks, calls = dialog
    item._exporting = True
    item._account_name = name
    item._confirmation = Widget("transfer-password")
    item._submit()
    pending.pop()()
    item._poll_result()
    assert calls == [("export", ("selected.asxaccount", "transfer-password", "codex"), {"account_name": name})]
    assert "refresh" not in events
    assert item._primary.options["state"] == "disabled"
    assert item._confirmation.get() == ""


@pytest.mark.parametrize("path,password,confirmation,exporting", [
    ("", "password", "password", True),
    ("file.asxaccount", "short", "short", True),
    ("file.asxaccount", "password1", "password2", True),
    ("file.asxaccount", "", "mismatch", True),
])
def test_invalid_form_never_starts_worker(dialog, path, password, confirmation, exporting):
    item, events, pending, _callbacks, _calls = dialog
    item._path.value = path
    item._password.value = password
    item._confirmation = Widget(confirmation)
    item._exporting = exporting
    item._submit()
    assert not pending and not events and not item._busy
    assert item._status.options["text"]


def test_import_accepts_older_short_nonempty_password(dialog):
    item, _events, pending, _callbacks, _calls = dialog
    item._password.value = "old"
    item._submit()
    assert len(pending) == 1


@pytest.mark.parametrize("exporting", [False, True])
def test_empty_password_is_forwarded_without_extra_confirmation(dialog, exporting):
    item, _events, pending, _callbacks, calls = dialog
    item._exporting = exporting
    item._password.value = ""
    item._confirmation = Widget("") if exporting else None
    item._submit()
    assert len(pending) == 1
    pending.pop()()
    item._poll_result()
    assert calls[0][1][1] == ""
    if exporting:
        assert "未加密" in item._status.options["text"]
        assert "加密登录包已导出" not in item._status.options["text"]


def test_empty_password_error_does_not_replace_empty_substring(dialog, monkeypatch):
    item, _events, pending, _callbacks, _calls = dialog
    item._password.value = ""

    def failed(*_args, **_kwargs):
        raise ValueError("该文件已加密，请输入正确密码")

    monkeypatch.setattr(module, "account_transfer", SimpleNamespace(import_account_login=failed))
    item._submit()
    pending.pop()()
    item._poll_result()
    assert item._status.options["text"] == "操作失败：该文件已加密，请输入正确密码"


def test_global_critical_operation_rejects_start(dialog):
    item, _events, pending, _callbacks, _calls = dialog
    item._owner._begin_critical_operation = lambda *_args: False
    item._submit()
    assert not pending and not item._busy and not item._critical_owned


def test_thread_start_failure_restores_buttons_and_critical_state(dialog, monkeypatch):
    item, events, _pending, _callbacks, _calls = dialog

    class FailingThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("no thread")

    monkeypatch.setattr(module.threading, "Thread", FailingThread)
    item._submit()
    assert not item._busy and not item._critical_owned
    assert ("end", "test-transfer") in events
    assert item._cancel.options["state"] == "normal"
    assert "未能启动" in item._status.options["text"]


def test_initial_timer_failure_does_not_run_crypto(dialog):
    item, events, pending, _callbacks, calls = dialog
    item.after = lambda *_args: (_ for _ in ()).throw(RuntimeError("timer unavailable"))
    item._submit()
    assert not pending and not calls and not item._busy
    assert ("end", "test-transfer") in events


def test_ui_setup_failure_releases_critical_state(dialog):
    item, events, pending, _callbacks, _calls = dialog
    original = item._set_busy

    def fail_only_when_busy(busy):
        if busy:
            raise RuntimeError("UI unavailable")
        original(busy)

    item._set_busy = fail_only_when_busy
    item._submit()
    assert not pending and not item._busy and not item._critical_owned
    assert ("end", "test-transfer") in events


def test_midflight_after_failure_uses_idle_fallback(dialog):
    item, _events, pending, _callbacks, _calls = dialog
    item._submit()
    item.after = lambda *_args: (_ for _ in ()).throw(RuntimeError("timer unavailable"))
    item._poll_result()
    assert item._poll_after_id == "idle"
    pending.pop()()
    item._poll_result()
    assert not item._busy


def test_scheduler_and_dispatch_failure_can_be_recovered_by_check_result(dialog, monkeypatch):
    item, events, pending, _callbacks, _calls = dialog
    item._submit()
    item.after = lambda *_args: (_ for _ in ()).throw(RuntimeError("timer unavailable"))
    item.after_idle = item.after
    monkeypatch.setattr(module, "run_on_ui_thread", lambda *_args: False)
    item._poll_result()
    assert item._cancel.options == {"state": "normal", "text": "检查结果"}
    item._close()
    assert "destroy" not in events
    pending.pop()()
    item._close()
    assert not item._busy and not item._critical_owned
    assert item._primary.options["text"] == "切换到此账号"


def test_closed_host_releases_only_python_critical_registry(dialog, monkeypatch):
    item, events, pending, _callbacks, _calls = dialog
    item._submit()
    item._on_destroy(SimpleNamespace(widget=item))
    monkeypatch.setattr(module, "run_on_ui_thread", lambda *_args: False)
    pending.pop()()
    assert ("abandon", "test-transfer") in events
    assert ("end", "test-transfer") not in events


def test_backend_failure_redacts_password_and_allows_retry(dialog, monkeypatch):
    item, events, pending, _callbacks, _calls = dialog

    def failed(*_args, **_kwargs):
        raise ValueError("Invalid transfer-password")

    monkeypatch.setattr(module, "account_transfer", SimpleNamespace(import_account_login=failed))
    item._submit()
    pending.pop()()
    item._poll_result()
    assert "transfer-password" not in item._status.options["text"]
    assert not item._busy and not item._critical_owned and item._result is None
    assert "refresh" not in events
    assert item._primary.options["state"] == "normal"


@pytest.mark.parametrize("error_text", ["", "   "])
def test_empty_backend_exception_is_failure_not_success(dialog, monkeypatch, error_text):
    item, events, pending, _callbacks, _calls = dialog

    def failed(*_args, **_kwargs):
        raise RuntimeError(error_text)

    monkeypatch.setattr(module, "account_transfer", SimpleNamespace(import_account_login=failed))
    item._submit()
    pending.pop()()
    item._poll_result()
    assert not item._busy and not item._critical_owned and item._result is None
    assert "登录包处理失败" in item._status.options["text"]
    assert "refresh" not in events
    assert item._primary.options["state"] == "normal"


@pytest.mark.parametrize("tab_module,tab_class,kind", [
    (claude_tab, claude_tab.ClaudeTab, "claude"),
    (codex_tab, codex_tab.CodexTab, "codex"),
])
def test_tab_actions_wire_current_saved_and_import(monkeypatch, tab_module, tab_class, kind):
    calls = []
    monkeypatch.setattr(tab_module, "open_account_transfer", lambda *args, **kwargs: calls.append((args, kwargs)))
    tab = SimpleNamespace()
    tab_class._export_account_login(tab)
    tab_class._export_account_login(tab, "Saved")
    tab_class._import_account_login(tab)
    assert calls == [
        ((tab, kind), {"exporting": True, "account_name": None}),
        ((tab, kind), {"exporting": True, "account_name": "Saved"}),
        ((tab, kind), {"exporting": False}),
    ]


def test_open_dialog_deduplicates_and_does_not_override_critical_operation(monkeypatch):
    created, events = [], []
    top = SimpleNamespace(_active_critical_operation_label=lambda: "busy")
    tab = SimpleNamespace(winfo_toplevel=lambda: top)
    monkeypatch.setattr(module, "AccountTransferDialog", lambda *args, **kwargs: created.append((args, kwargs)))
    monkeypatch.setattr(module, "show_toast", lambda *_args, **_kwargs: events.append("blocked"))
    module.open_account_transfer(tab, "claude", exporting=True)
    assert events == ["blocked"] and not created
    top._active_critical_operation_label = lambda: ""
    tab._account_transfer_dialog = SimpleNamespace(
        winfo_exists=lambda: True, lift=lambda: events.append("lift"), focus_set=lambda: None,
    )
    module.open_account_transfer(tab, "claude", exporting=True)
    assert not created and events[-1] == "lift"


def test_import_completion_closure_reuses_existing_switch_preview(monkeypatch):
    created, events = [], []
    tab = SimpleNamespace(
        winfo_toplevel=lambda: SimpleNamespace(), _destroyed=False,
        refresh=lambda: events.append("refresh"),
        _refresh_shell_state=lambda: events.append("shell"),
        _switch_account=lambda name: events.append(("switch", name)),
    )
    monkeypatch.setattr(module, "AccountTransferDialog", lambda *args, **kwargs: created.append((args, kwargs)))
    module.open_account_transfer(tab, "codex", exporting=False)
    callbacks = created[0][1]
    callbacks["on_imported"]()
    assert events == ["refresh", "shell"]
    callbacks["on_activate"]("selected")
    assert events[-1] == ("switch", "selected")
    tab._destroyed = True
    callbacks["on_activate"]("ignored")
    assert events[-1] == ("switch", "selected")


def test_import_ui_refresh_failure_preserves_success_and_activation(dialog):
    item, _events, pending, _callbacks, _calls = dialog
    item._on_imported = lambda: (_ for _ in ()).throw(RuntimeError("tab gone"))
    item._submit()
    pending.pop()()
    item._poll_result()
    assert item._result is not None
    assert item._primary.options["state"] == "normal"
    assert "已保存" in item._status.options["text"]


def test_critical_ui_restore_failure_does_not_lock_transfer_dialog(dialog):
    item, events, pending, _callbacks, _calls = dialog
    item._owner._end_critical_operation = lambda _key: (_ for _ in ()).throw(RuntimeError("owner widget gone"))
    item._submit()
    pending.pop()()
    item._poll_result()
    assert not item._busy and not item._critical_owned
    assert ("abandon", "test-transfer") in events
    assert item._primary.options["text"] == "切换到此账号"


@pytest.mark.parametrize("exporting", [False, True])
@pytest.mark.parametrize("kind", ["codex", "claude"])
def test_native_transfer_footer_stays_visible_in_small_window(exporting, kind, tk_root):
    import customtkinter as ctk
    import time

    root = ctk.CTkToplevel(tk_root)
    root.geometry("680x560")
    window = None
    try:
        window = module.AccountTransferDialog(root, kind, exporting=exporting, on_activate=lambda _name: None)
        window.geometry("440x360")
        # Windows maps a new transient asynchronously; one update can run
        # before its owner receives the Map event on a busy release-test host.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            root.update()
            if window._primary.winfo_ismapped() and window._cancel.winfo_ismapped():
                break
            time.sleep(0.01)
        for button in (window._primary, window._cancel):
            assert button.winfo_ismapped(), {
                "owner_state": root.state(), "window_state": window.state(),
                "owner_mapped": root.winfo_ismapped(), "window_mapped": window.winfo_ismapped(),
                "geometry": window.geometry(), "button_grid": button.grid_info(),
                "footer_geometry": button.master.winfo_geometry(),
            }
            assert button.winfo_rootx() >= window.winfo_rootx()
            assert button.winfo_rootx() + button.winfo_width() <= window.winfo_rootx() + window.winfo_width()
            assert button.winfo_rooty() + button.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        assert window._path.cget("state") == "disabled"
        # CTk removes masking while displaying a placeholder, then restores it
        # before accepting actual entry text.
        window._password.insert(0, "synthetic-password")
        assert window._password.cget("show") == "*"
        if window._confirmation is not None:
            window._confirmation.insert(0, "synthetic-password")
            assert window._confirmation.cget("show") == "*"
    finally:
        if window is not None:
            window.destroy()
        root.destroy()


@pytest.mark.parametrize("tab_class", [claude_tab.ClaudeTab, codex_tab.CodexTab])
def test_native_account_actions_fit_narrow_tab(monkeypatch, tab_class, tk_root):
    import customtkinter as ctk

    monkeypatch.setattr(tab_class, "refresh", lambda _self: None)
    root = ctk.CTkToplevel(tk_root)
    root.geometry("440x650")
    tab = None
    try:
        tab = tab_class(root)
        tab.pack(fill="both", expand=True)
        root.update()
        tab._apply_responsive_layout()
        root.update()
        actions = (tab._account_import_button, tab._account_export_login_button, tab._account_import_login_button)
        for button in actions:
            assert button.winfo_ismapped()
            assert button.winfo_rootx() >= tab.winfo_rootx()
            assert button.winfo_rootx() + button.winfo_width() <= tab.winfo_rootx() + tab.winfo_width()
        assert tab._account_actions.winfo_y() >= tab._account_title.winfo_y() + tab._account_title.winfo_height()
    finally:
        if tab is not None:
            tab.destroy()
        root.destroy()


def test_native_standalone_worker_completes_through_main_thread_poll(monkeypatch, tk_root):
    import threading
    import time
    import customtkinter as ctk

    events = []
    owner = ctk.CTkToplevel(tk_root)
    window = None

    def import_login(*_args, **_kwargs):
        assert threading.current_thread() is not threading.main_thread()
        events.append("worker")
        return SimpleNamespace(account_name="Synthetic imported account", profile_type="claude")

    def refresh():
        assert threading.current_thread() is threading.main_thread()
        events.append("refresh")

    monkeypatch.setattr(module, "account_transfer", SimpleNamespace(import_account_login=import_login))
    try:
        window = module.AccountTransferDialog(owner, "claude", exporting=False, on_imported=refresh)
        window._path.configure(state="normal")
        window._path.insert(0, "synthetic-only.asxaccount")
        window._path.configure(state="disabled")
        window._password.insert(0, "synthetic-password")
        window._submit()
        deadline = time.monotonic() + 3
        while window._busy and time.monotonic() < deadline:
            tk_root.update()
            time.sleep(0.005)
        assert not window._busy
        assert events == ["worker", "refresh"]
        assert window._result.account_name == "Synthetic imported account"
        assert window._password.get() == ""
    finally:
        if window is not None:
            window.destroy()
        owner.destroy()


def test_native_optional_password_export_button_and_warning(tk_root, monkeypatch):
    import customtkinter as ctk

    owner = ctk.CTkToplevel(tk_root)
    window = None
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    try:
        window = module.AccountTransferDialog(owner, "codex", exporting=True)
        assert window._primary.cget("text") == "无密码导出"
        assert "任何获得文件的人都可读取其中凭据" in window._password_notice.cget("text")
        window._password.insert(0, "synthetic-password")
        tk_root.update()
        assert window._primary.cget("text") == "加密导出"
        window._password.delete(0, "end")
        tk_root.update()
        assert window._primary.cget("text") == "无密码导出"
        window._password.insert(0, "synthetic-password")
        password_var = window._password_var
        window._close()
        assert password_var.get() == ""
        assert errors == []
        window = None
    finally:
        if window is not None:
            window.destroy()
        owner.destroy()


def capture_preview(directory):
    """Capture synthetic HWNDs only; never read account settings or the desktop."""
    from pathlib import Path
    import time

    import customtkinter as ctk
    from tools.ui_visual_audit import capture_window_image
    from ui.dialogs.password_dialog import PasswordDialog

    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.title("隔离账号迁移预览（合成数据）")
    root.geometry("680x580")
    try:
        for mode in ("export", "imported"):
            window = module.AccountTransferDialog(root, "codex", exporting=mode == "export", on_activate=lambda _name: None)
            if mode == "imported":
                window._finish(SimpleNamespace(account_name="合成演示账号", profile_type="codex"), None)
            window.lift()
            for _ in range(8):
                root.update()
                time.sleep(0.03)
            capture_window_image(window).save(destination / f"account-transfer-{mode}.png")
            window.destroy()
        window = PasswordDialog(
            root, title="导出迁移包（密码可选）",
            message="迁移包只会包含已选择的 Profile、其引用密钥及所选浏览器 Profile 的登录数据；不会包含浏览器缓存、组件模型或普通运行日志。可留空直接导出；加密导出需设置至少 8 个字符的密码。",
            on_confirm=lambda _password: None, confirm_password=True, allow_empty=True,
        )
        for _ in range(8):
            root.update()
            time.sleep(0.03)
        capture_window_image(window).save(destination / "optional-migration-password.png")
        window.destroy()
    finally:
        root.destroy()


if __name__ == "__main__":
    import sys
    capture_preview(sys.argv[1])
