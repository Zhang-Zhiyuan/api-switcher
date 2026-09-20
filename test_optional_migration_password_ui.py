"""Optional passwords remain explicit and never mislabel plaintext as encrypted."""

from types import SimpleNamespace

import pytest

from ui.dialogs.password_dialog import PasswordDialog
from ui.tabs import backup_tab as module
from test_account_transfer_ui import Widget


@pytest.mark.parametrize("allow_empty,exporting,password,confirmation,valid", [
    (False, True, "", "", False),
    (False, False, "", "", False),
    (False, True, "short", "short", False),
    (False, True, "password", "password", True),
    (True, True, "", "", True),
    (True, False, "", "", True),
    (True, True, "", "password", False),
    (True, True, "short", "short", False),
    (True, True, "password", "mismatch", False),
    (True, True, "password", "password", True),
    (True, False, "short", "", True),
])
def test_password_validation_is_opt_in_and_preserves_exact_input(allow_empty, exporting, password, confirmation, valid):
    dialog = object.__new__(PasswordDialog)
    dialog._password = Widget(password)
    dialog._password_confirm = Widget(confirmation) if exporting else None
    dialog._confirm_password = exporting
    dialog._allow_empty = allow_empty
    dialog._error = Widget()
    events = []
    dialog._on_confirm = lambda value: events.append(value)
    dialog.destroy = lambda: events.append("closed")
    dialog._confirm()
    assert events == (["closed", password] if valid else [])
    if not valid:
        assert dialog._error.options["text"]


@pytest.mark.parametrize("exporting", [False, True])
def test_native_optional_password_dialog_labels_warning_and_empty_submit(tk_root, exporting):
    import customtkinter as ctk

    owner = ctk.CTkToplevel(tk_root)
    window = None
    calls = []
    try:
        window = PasswordDialog(owner, title="合成迁移测试", message="仅测试界面，不写入真实设置",
                                on_confirm=calls.append, confirm_password=exporting, allow_empty=True)
        window.geometry("430x260")
        tk_root.update()
        button = window._confirm_button
        assert button.winfo_rooty() + button.winfo_height() <= window.winfo_rooty() + window.winfo_height()
        assert "请勿公开分享" in window._password_notice.cget("text")
        assert button.cget("text") == ("无密码导出" if exporting else "导入")
        window._password.insert(0, "synthetic-password")
        tk_root.update()
        assert button.cget("text") == ("加密导出" if exporting else "导入")
        window._password.delete(0, "end")
        window._confirm()
        assert calls == [""]
        window = None
    finally:
        if window is not None:
            window.destroy()
        owner.destroy()


def test_native_password_dialog_cancel_discards_password_variable(tk_root, monkeypatch):
    import customtkinter as ctk

    owner = ctk.CTkToplevel(tk_root)
    window = None
    calls = []
    errors = []
    monkeypatch.setattr(tk_root, "report_callback_exception", lambda *args: errors.append(args))
    try:
        window = PasswordDialog(owner, title="合成迁移测试", message="仅测试界面",
                                on_confirm=calls.append, confirm_password=True, allow_empty=True)
        window._password.insert(0, "synthetic-password")
        window._password_confirm.insert(0, "synthetic-password")
        password_var = window._password_var
        window.destroy()
        window = None
        assert password_var.get() == ""
        assert calls == []
        assert errors == []
    finally:
        if window is not None:
            window.destroy()
        owner.destroy()


@pytest.mark.parametrize("text,password,expected", [
    ("needs password", "", "needs password"),
    ("password synthetic-password failed", "synthetic-password", "password [密码已隐藏] failed"),
    ("", "", "迁移文件处理失败，请检查文件与密码后重试"),
])
def test_backup_errors_do_not_leak_password_or_replace_empty_string(text, password, expected):
    assert module._migration_error_text(ValueError(text), password) == expected


@pytest.mark.parametrize("direction", ["export", "import"])
@pytest.mark.parametrize("password", ["", "synthetic-password"])
def test_zip_ui_allows_optional_password_and_forwards_to_backend(monkeypatch, direction, password):
    prompts, calls, toasts, confirms = [], [], [], []
    top = SimpleNamespace(refresh_all=lambda: None)
    tab = object.__new__(module.BackupTab)
    tab.winfo_toplevel = lambda: top
    tab._portable_operation_in_progress = False
    result = SimpleNamespace(profile_count=1, secret_count=1, missing_secret_refs=[], skipped_secret_refs=[])
    summary = SimpleNamespace(profile_count=1, secret_count=1, missing_secret_count=0,
                              created_at="", encrypted=bool(password))
    monkeypatch.setattr(module, "PasswordDialog", lambda _top, **kwargs: prompts.append(kwargs))
    monkeypatch.setattr(module, "ConfirmDialog", lambda _top, **kwargs: confirms.append(kwargs))
    monkeypatch.setattr(module, "show_toast", lambda _top, message, **_kwargs: toasts.append(message))
    monkeypatch.setattr(module.filedialog, "asksaveasfilename", lambda **_kwargs: "synthetic.zip")
    monkeypatch.setattr(module.filedialog, "askopenfilename", lambda **_kwargs: "synthetic.zip")
    monkeypatch.setattr(module, "local_config_bundle", SimpleNamespace(
        export_local_config_zip=lambda *args: calls.append(args) or result,
        import_local_config_zip=lambda *args: calls.append(args) or result,
        inspect_local_config_zip=lambda _path: summary,
    ))
    if direction == "export":
        tab._export_local_config_zip()
    else:
        tab._import_local_config_zip()
        assert ("未加密" if not password else "已加密") in confirms[0]["message"]
        confirms[0]["on_confirm"]()
    assert prompts[0]["allow_empty"] is True
    prompts[0]["on_confirm"](password)
    assert calls == [("synthetic.zip", password)]
    if direction == "export":
        assert ("未加密" if not password else "已加密") in toasts[-1]


@pytest.mark.parametrize("direction", ["export", "import"])
def test_portable_ui_forwards_empty_password_with_explicit_plaintext_feedback(monkeypatch, direction):
    prompts, calls, toasts = [], [], []
    top = SimpleNamespace(refresh_all=lambda: None)
    tab = object.__new__(module.BackupTab)
    tab.winfo_toplevel = lambda: top
    result = SimpleNamespace(profile_count=1, secret_count=1, missing_secret_refs=[], browser_file_count=0,
                             skipped_browser_files=[])
    monkeypatch.setattr(module, "PasswordDialog", lambda _top, **kwargs: prompts.append(kwargs))
    monkeypatch.setattr(module, "show_toast", lambda _top, message, **_kwargs: toasts.append(message))
    monkeypatch.setattr(module, "PortableExportSelectionDialog",
                        lambda _top, **kwargs: kwargs["on_confirm"]({"claude_profiles": ["synthetic"]}))
    monkeypatch.setattr(module.filedialog, "asksaveasfilename", lambda **_kwargs: "synthetic.asxprofile")
    monkeypatch.setattr(module.filedialog, "askopenfilename", lambda **_kwargs: "synthetic.asxprofile")
    monkeypatch.setattr(module, "portable_migration", SimpleNamespace(
        list_portable_profile_options=lambda: {"claude_profiles": ["synthetic"]},
        export_portable_profiles=lambda *args, **_kwargs: calls.append(args) or result,
        import_portable_profiles=lambda *args: calls.append(args) or result,
    ))
    monkeypatch.setattr(module.threading, "Thread", lambda *, target, **_kwargs: SimpleNamespace(start=target))
    monkeypatch.setattr(module, "run_on_ui_thread", lambda _top, callback: callback())
    if direction == "export":
        tab._export_portable()
    else:
        tab._import_portable()
    assert prompts[0]["allow_empty"] is True
    prompts[0]["on_confirm"]("")
    assert calls == [("synthetic.asxprofile", "")]
    if direction == "export":
        assert "未加密" in toasts[-1]
        assert "请勿公开分享" in toasts[-1]
