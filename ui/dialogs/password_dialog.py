import customtkinter as ctk

from ui.dialogs.confirm_dialog import _bind_two_button_footer
from ui.theme import COLORS, bind_wraplength, button_style, center_window, font, input_style


class PasswordDialog(ctk.CTkToplevel):
    """Password prompt used for portable profile export/import."""

    def __init__(self, master, title: str, message: str, on_confirm, confirm_password: bool = False,
                 allow_empty: bool = False):
        super().__init__(master)
        self.title(title)
        height = (410 if confirm_password else 350) if allow_empty else (310 if confirm_password else 250)
        self.geometry(f"480x{height}")
        self.minsize(420, 230)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_confirm = on_confirm
        self._confirm_password = confirm_password
        self._allow_empty = allow_empty
        self._password_var = ctk.StringVar(master=self, value="")
        self._password_trace = None

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(side="bottom", fill="x", padx=20, pady=(6, 18))

        body = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        body.pack(fill="both", expand=True, padx=14, pady=(12, 0))

        ctk.CTkLabel(
            body,
            text=title,
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        message_label = ctk.CTkLabel(
            body,
            text=message,
            justify="left",
            text_color=COLORS["muted"],
            font=font(12),
        )
        message_label.pack(fill="x", anchor="w", pady=(0, 12))
        bind_wraplength(body, message_label, padding=4, min_width=280, max_width=560)

        if allow_empty:
            note = (
                "密码可留空；留空时未加密。\n任何获得文件的人都可读取其中凭据，请勿公开分享。"
                if confirm_password else
                "无密码包可留空导入；加密包仍需正确密码。未加密文件中的凭据可被直接读取，请勿公开分享。"
            )
            self._password_notice = ctk.CTkLabel(
                body, text=note, justify="left", anchor="w", text_color=COLORS["warning"], font=font(12),
            )
            self._password_notice.pack(fill="x", pady=(0, 10))
            bind_wraplength(body, self._password_notice, padding=4, min_width=280, max_width=560)

        ctk.CTkLabel(
            body, text="迁移密码（可留空）" if allow_empty else "迁移密码",
            anchor="w", font=font(12), text_color=COLORS["muted"],
        ).pack(fill="x", pady=(0, 4))
        self._password = ctk.CTkEntry(
            body,
            width=1,
            show="*",
            textvariable=self._password_var,
            placeholder_text="迁移密码（可留空）" if allow_empty else "迁移密码",
            **input_style(),
        )
        self._password.pack(fill="x", pady=(0, 8))
        self._password.focus_set()

        self._password_confirm = None
        if confirm_password:
            self._password_confirm = ctk.CTkEntry(
                body,
                width=1,
                show="*",
                placeholder_text="再次输入迁移密码",
                **input_style(),
            )
            self._password_confirm.pack(fill="x", pady=(0, 8))

        self._error = ctk.CTkLabel(body, text="", text_color=COLORS["danger"], font=font(12), anchor="w")
        self._error.pack(fill="x")

        cancel_button = ctk.CTkButton(
            btn_frame,
            text="取消",
            width=1,
            command=self.destroy,
            **button_style("secondary"),
        )
        self._confirm_button = ctk.CTkButton(
            btn_frame,
            text="确定",
            width=1,
            command=self._confirm,
            **button_style("primary"),
        )
        _bind_two_button_footer(btn_frame, self._confirm_button, cancel_button)
        if allow_empty:
            self._password_trace = self._password_var.trace_add("write", self._update_action_label)
            self._update_action_label()

        self.bind("<Return>", lambda _event: self._confirm())
        center_window(self, master)

    def _confirm(self):
        password = self._password.get()
        allow_empty = getattr(self, "_allow_empty", False)
        if (not allow_empty and len(password) < 8) or (
            allow_empty and self._confirm_password and password and len(password) < 8
        ):
            self._error.configure(text="迁移密码至少需要 8 个字符")
            return
        if self._confirm_password and self._password_confirm:
            if password != self._password_confirm.get():
                self._error.configure(text="两次输入的迁移密码不一致")
                return

        self.destroy()
        self._on_confirm(password)

    def _update_action_label(self, *_args):
        text = "导入"
        if self._confirm_password:
            text = "加密导出" if self._password_var.get() else "无密码导出"
        self._confirm_button.configure(text=text)

    def destroy(self):
        if self._password_trace is not None:
            try:
                self._password_var.trace_remove("write", self._password_trace)
            except Exception:
                pass
            self._password_trace = None
        try:
            # CTkEntry also registers a trace. Drop every observer owned by this
            # private variable before clearing it as the dialog is torn down.
            for modes, callback in self._password_var.trace_info():
                self._password_var.trace_remove(modes, callback)
            self._password_var.set("")
            if self._password_confirm is not None:
                self._password_confirm.delete(0, "end")
        except Exception:
            pass
        super().destroy()
