"""Single-account portable login transfer without touching active credentials."""

from __future__ import annotations

import queue
import threading
from tkinter import filedialog

import customtkinter as ctk

from core.lazy_imports import LazyModule
from ui.dialogs.confirm_dialog import _bind_two_button_footer
from ui.feedback import safe_feedback_text
from ui.theme import COLORS, bind_wraplength, button_style, center_window, font, input_style
from ui.ui_dispatch import run_on_ui_thread
from ui.widgets.toast import show_toast


account_transfer = LazyModule("core.account_transfer")


def open_account_transfer(tab, profile_type: str, *, exporting: bool, account_name=None):
    """Keep only one transfer dialog per tab and never activate an import implicitly."""
    top = tab.winfo_toplevel()
    active = getattr(top, "_active_critical_operation_label", None)
    if callable(active) and active():
        show_toast(top, "另一个关键数据操作正在处理，请稍候", is_error=True)
        return
    existing = getattr(tab, "_account_transfer_dialog", None)
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.lift()
                existing.focus_set()
                return
        except Exception:
            pass

    def imported():
        if not getattr(tab, "_destroyed", False):
            tab.refresh()
            tab._refresh_shell_state()

    def activate(name):
        if not getattr(tab, "_destroyed", False):
            tab._switch_account(name)

    tab._account_transfer_dialog = AccountTransferDialog(
        top, profile_type, exporting=exporting, account_name=account_name,
        on_imported=imported, on_activate=activate,
    )


class AccountTransferDialog(ctk.CTkToplevel):
    """Password entry, background crypto and explicit post-import activation."""

    def __init__(self, master, profile_type: str, *, exporting: bool,
                 account_name=None, on_imported=None, on_activate=None):
        if profile_type not in {"claude", "codex"}:
            raise ValueError("不支持的账号类型")
        super().__init__(master)
        self._owner = master
        self._profile_type = profile_type
        self._exporting = exporting
        self._account_name = account_name
        self._on_imported = on_imported
        self._on_activate = on_activate
        self._busy = False
        self._destroyed = False
        self._result = None
        self._critical_key = f"account-login-transfer-{id(self)}"
        self._critical_owned = False
        self._messages = queue.Queue()
        self._poll_after_id = None
        self._poll_failed = False
        self._export_encrypted = False
        self._password_var = ctk.StringVar(master=self, value="")
        self._password_trace = None
        self._show_after_id = None

        product = "Claude" if profile_type == "claude" else "Codex"
        self._title = f"{'导出' if exporting else '导入'} {product} 登录包"
        self.title(self._title)
        self.geometry("600x520" if exporting else "600x470")
        self.minsize(430, 340)
        self.configure(fg_color=COLORS["app_bg"])
        # center_window sets the transient owner after constructing the body.
        # Doing it during CTk's initial Windows titlebar withdraw/redraw can
        # leave a newly opened child permanently hidden with an active grab.
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<Destroy>", self._on_destroy, add="+")

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=20, pady=(8, 18))
        self._primary = ctk.CTkButton(
            footer, text="无密码导出" if exporting else "导入并保存", width=1,
            command=self._submit, **button_style("primary"),
        )
        self._cancel = ctk.CTkButton(
            footer, text="取消", width=1, command=self._close, **button_style("secondary"),
        )
        _bind_two_button_footer(footer, self._primary, self._cancel)
        body = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        body.pack(fill="both", expand=True, padx=14, pady=(14, 0))
        ctk.CTkLabel(body, text=self._title, font=font(17, "bold"),
                     text_color=COLORS["text"]).pack(anchor="w", pady=(0, 8))
        if exporting:
            source = f"已保存账号：{account_name}" if account_name else "本机当前官方登录（不导出 API 密钥）"
            detail = (
                f"{source}\n仅包含这一个账号的登录状态，不包含其他账号、代理或机器设置。"
                "\n请只迁移你自己的账号，并通过可信方式传输登录包；如设置密码，请另行保管。"
                "\n已保存快照可能早于当前登录，建议优先导出当前登录。"
            )
        else:
            detail = (
                f"导入一个 {product} 登录包，保留其他账号及当前 API / 登录状态。"
                "\n导入后先保存为独立账号，再由你点击“切换到此账号”确认使用。"
                "\n请只导入你自己的账号，不要接收不可信来源的登录包。"
            )
        if profile_type == "codex":
            detail += (
                "\n迁移后请停止源端使用这份登录；多机长期使用请分别登录。"
                "\n切换前结束任务并退出 Codex / VS Code，完成后再打开。"
                "\n凭据过期或已使用时须重新登录，再导出新包；重复导入旧包无效。"
            )
        else:
            detail += "\n登录过期或被撤销仍需重新登录；多台电脑刷新同一凭据可能使旧副本失效。"
        label = ctk.CTkLabel(body, text=detail, anchor="w", justify="left",
                             font=font(12), text_color=COLORS["muted"])
        label.pack(fill="x", pady=(0, 12))
        bind_wraplength(body, label, padding=4, min_width=260, max_width=850)
        notice = (
            "密码可留空。留空时未加密，任何获得文件的人都可读取其中凭据，请勿公开分享。"
            if exporting else "无密码包可留空导入；加密包仍需正确密码。未加密文件中的凭据可被直接读取，请勿公开分享。"
        )
        self._password_notice = ctk.CTkLabel(body, text=notice, anchor="w", justify="left",
                                           font=font(12), text_color=COLORS["warning"])
        self._password_notice.pack(fill="x", pady=(0, 10))
        bind_wraplength(body, self._password_notice, padding=4, min_width=260, max_width=850)

        file_row = ctk.CTkFrame(body, fg_color="transparent")
        file_row.pack(fill="x", pady=(0, 10))
        file_row.grid_columnconfigure(0, weight=1)
        self._path = ctk.CTkEntry(file_row, width=1, placeholder_text="选择 .asxaccount 登录包", **input_style())
        self._path.grid(row=0, column=0, sticky="ew")
        # Use the native save dialog for overwrite confirmation; a typed path must
        # not bypass its safety prompt.
        self._path.configure(state="disabled")
        self._browse = ctk.CTkButton(file_row, text="选择文件", width=82, command=self._choose_path,
                                    **button_style("secondary"))
        self._browse.grid(row=0, column=1, padx=(8, 0))
        password_label = "迁移密码（可留空；加密导出至少 8 字符）" if exporting else "迁移密码（无密码包请留空）"
        ctk.CTkLabel(body, text=password_label, anchor="w",
                     font=font(12), text_color=COLORS["muted"]).pack(fill="x", pady=(0, 4))
        self._password = ctk.CTkEntry(body, width=1, show="*", textvariable=self._password_var,
                                     placeholder_text=password_label, **input_style())
        self._password.pack(fill="x", pady=(0, 8))
        self._confirmation = None
        if exporting:
            self._confirmation = ctk.CTkEntry(body, width=1, show="*",
                                             placeholder_text="再次输入迁移密码", **input_style())
            self._confirmation.pack(fill="x", pady=(0, 8))
        self._status = ctk.CTkLabel(body, text="", anchor="w", justify="left", font=font(12))
        self._status.pack(fill="x", pady=(4, 6))
        bind_wraplength(body, self._status, padding=4, min_width=260, max_width=850)
        self._inputs = [self._browse, self._password]
        if self._confirmation is not None:
            self._inputs.append(self._confirmation)
        self._password_trace = self._password_var.trace_add("write", self._update_action_label)
        center_window(self, master)
        self.grab_set()
        # CTk temporarily withdraws native Windows windows to repaint their
        # titlebar. A new transient can inherit that hidden state from its
        # owner. Reveal it once the initial callbacks have settled, so an
        # invisible dialog cannot keep the application grabbed.
        self._show_after_id = self.after(50, self._reveal_after_initial_layout)

    def _reveal_after_initial_layout(self):
        self._show_after_id = None
        if (not self._destroyed and self.winfo_exists() and self._owner.winfo_exists()
                and self._owner.winfo_viewable() and self.state() == "withdrawn"):
            self.deiconify()
            self.lift()

    def _choose_path(self):
        if self._busy or self._result is not None:
            return
        options = dict(parent=self, title=self._title,
                       filetypes=[("账号登录包", "*.asxaccount")])
        if self._exporting:
            value = filedialog.asksaveasfilename(
                **options, defaultextension=".asxaccount", initialfile=f"{self._profile_type}-login.asxaccount",
            )
        else:
            value = filedialog.askopenfilename(**options)
        if value:
            self._path.configure(state="normal")
            self._path.delete(0, "end")
            self._path.insert(0, value)
            self._path.configure(state="disabled")

    def _set_status(self, text, *, error=False):
        self._status.configure(text=safe_feedback_text(text),
                               text_color=COLORS["danger"] if error else COLORS["muted"])

    def _set_busy(self, busy):
        self._busy = busy
        for widget in self._inputs:
            try:
                widget.configure(state="disabled" if busy else "normal")
            except Exception:
                pass
        self._primary.configure(state="disabled" if busy else "normal",
                                text="处理中…" if busy else self._action_label())
        self._cancel.configure(state="disabled" if busy else "normal")

    def _action_label(self):
        if self._exporting:
            return "加密导出" if self._password.get() else "无密码导出"
        return "导入并保存"

    def _update_action_label(self, *_args):
        if not self._destroyed and not self._busy and self._result is None:
            self._primary.configure(text=self._action_label())

    def _submit(self):
        if self._busy or self._destroyed or self._result is not None:
            return
        path, password = self._path.get().strip(), self._password.get()
        if not path:
            self._set_status("请先选择登录包文件", error=True)
            return
        if self._exporting and password and len(password) < 8:
            self._set_status("加密导出密码至少需要 8 个字符；也可留空无密码导出", error=True)
            return
        if self._exporting and password != self._confirmation.get():
            self._set_status("两次输入的密码不一致", error=True)
            return
        begin = getattr(self._owner, "_begin_critical_operation", None)
        if callable(begin):
            if not begin(self._critical_key, f"正在{self._title}"):
                self._set_status("另一个关键数据操作正在处理，请稍候", error=True)
                return
            self._critical_owned = True
        self._poll_failed = False
        self._export_encrypted = bool(password)
        try:
            self._set_busy(True)
            progress = "正在后台加密导出…" if password else "正在后台无密码导出…文件未加密，请勿公开分享。"
            self._set_status(progress if self._exporting else "正在后台校验并导入…当前登录不会改变。")
            self._poll_after_id = self.after(80, self._poll_result)
        except Exception:
            try:
                self._finish(None, "界面暂时无法调度任务，请关闭窗口后重试")
            finally:
                self._busy = False
            return

        def worker():
            result, error = None, None
            try:
                if self._exporting:
                    result = account_transfer.export_account_login(
                        path, password, self._profile_type, account_name=self._account_name,
                    )
                else:
                    result = account_transfer.import_account_login(path, password, expected_type=self._profile_type)
            except Exception as exc:
                detail = str(exc)
                if password:
                    detail = detail.replace(password, "[密码已隐藏]")
                error = safe_feedback_text(detail).strip() or "登录包处理失败，请检查文件和密码后重试"
            self._messages.put((result, error))
            # Queue polling also works in a standalone Tk host without an App dispatcher.
            dispatched = run_on_ui_thread(self, self._poll_result)
            if not dispatched and self._destroyed:
                self._abandon_operation()

        try:
            threading.Thread(target=worker, name="account-login-transfer", daemon=True).start()
        except Exception:
            self._finish(None, "后台任务未能启动，请稍后重试")

    def _poll_result(self):
        if self._destroyed or not self._busy:
            return
        self._cancel_poll()
        try:
            result, error = self._messages.get_nowait()
        except queue.Empty:
            try:
                self._poll_after_id = self.after(80, self._poll_result)
            except Exception:
                try:
                    self._poll_after_id = self.after_idle(self._poll_result)
                except Exception:
                    self._poll_failed = True
                    self._cancel.configure(state="normal", text="检查结果")
                    self._set_status("自动刷新暂不可用。任务仍在后台进行，请点击“检查结果”查看完成情况；不要退出程序。")
            return
        self._finish(result, error)

    def _cancel_poll(self):
        timer, self._poll_after_id = self._poll_after_id, None
        if timer is not None:
            try:
                self.after_cancel(timer)
            except Exception:
                pass

    def _finish(self, result, error):
        self._cancel_poll()
        if self._critical_owned:
            self._critical_owned = False
            end = getattr(self._owner, "_end_critical_operation", None)
            if callable(end):
                try:
                    end(self._critical_key)
                except Exception:
                    abandon = getattr(self._owner, "_abandon_critical_operation", None)
                    if callable(abandon):
                        abandon(self._critical_key)
        if self._destroyed:
            return
        self._busy = False
        self._set_busy(False)
        # Remove password text after any attempt. Do not retain secrets in result objects.
        self._password.delete(0, "end")
        if self._confirmation is not None:
            self._confirmation.delete(0, "end")
        if error:
            self._set_status(f"操作失败：{error}", error=True)
            return
        self._result = result
        for widget in self._inputs:
            widget.configure(state="disabled")
        self._cancel.configure(text="关闭")
        if self._exporting:
            self._primary.configure(text="已导出", state="disabled")
            self._set_status(
                "加密登录包已导出。请在另一台电脑的对应账号页选择“导入登录包”，并输入同一密码。"
                if self._export_encrypted else
                "无密码登录包已导出。另一台电脑可直接导入。文件未加密，任何获得文件的人都可读取其中凭据，请勿公开分享。"
            )
        else:
            self._primary.configure(text="切换到此账号", command=self._activate,
                                    state="normal" if callable(self._on_activate) else "disabled")
            self._set_status(f"已保存账号：{result.account_name}\n当前登录和 API 未改动。点击“切换到此账号”查看切换预览并确认。")
            if callable(self._on_imported):
                try:
                    self._on_imported()
                except Exception:
                    self._set_status("登录包已保存，账号列表未能刷新；关闭后可重新刷新列表再切换。")
        warnings = tuple(getattr(result, "warnings", ()) or ())
        if self._profile_type == "codex":
            warnings += ("未验证服务端登录。若 refresh token 过期 / 已使用，请重新登录；重复导入旧包无效。",)
        if warnings:
            self._set_status(str(self._status.cget("text")) + "\n" + "\n".join(warnings))
            self._status.configure(text_color=COLORS["warning"])

    def _activate(self):
        if self._busy or self._destroyed or self._exporting or self._result is None:
            return
        name, activate = self._result.account_name, self._on_activate
        self.destroy()
        if callable(activate):
            activate(name)

    def _abandon_operation(self):
        """Worker-safe cleanup when Tk has gone away; never touches a widget."""
        if self._critical_owned:
            self._critical_owned = False
            abandon = getattr(self._owner, "_abandon_critical_operation", None)
            if callable(abandon):
                abandon(self._critical_key)

    def _close(self):
        if self._busy:
            # A working close button can drain completion even if both Tk scheduling
            # methods and the optional App dispatcher stopped accepting callbacks.
            self._poll_result()
            return
        self.destroy()

    def _on_destroy(self, event):
        if event.widget is self:
            self._destroyed = True
            show_after_id = self.__dict__.get("_show_after_id")
            if show_after_id is not None:
                try:
                    self.after_cancel(show_after_id)
                except Exception:
                    pass
                self._show_after_id = None
            trace = getattr(self, "_password_trace", None)
            if trace is not None:
                try:
                    self._password_var.trace_remove("write", trace)
                except Exception:
                    pass
                self._password_trace = None
            password_var = self.__dict__.get("_password_var")
            if password_var is not None:
                try:
                    # Child entries may already be destroyed when this event
                    # arrives; their CTk traces must not run against dead widgets.
                    for modes, callback in password_var.trace_info():
                        password_var.trace_remove(modes, callback)
                    password_var.set("")
                except Exception:
                    pass
            if self._confirmation is not None:
                try:
                    self._confirmation.delete(0, "end")
                except Exception:
                    pass
            self._cancel_poll()
            if not self._busy or not self._messages.empty():
                self._abandon_operation()
