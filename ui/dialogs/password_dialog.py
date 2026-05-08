import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, center_window, font, input_style


class PasswordDialog(ctk.CTkToplevel):
    """Password prompt used for portable profile export/import."""

    def __init__(self, master, title: str, message: str, on_confirm, confirm_password: bool = False):
        super().__init__(master)
        self.title(title)
        self.geometry("480x310" if confirm_password else "480x250")
        self.minsize(420, 230)
        self.resizable(True, False)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_confirm = on_confirm
        self._confirm_password = confirm_password

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(18, 12))

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

        self._password = ctk.CTkEntry(body, show="*", placeholder_text="迁移密码", **input_style())
        self._password.pack(fill="x", pady=(0, 8))
        self._password.focus_set()

        self._password_confirm = None
        if confirm_password:
            self._password_confirm = ctk.CTkEntry(body, show="*", placeholder_text="再次输入迁移密码", **input_style())
            self._password_confirm.pack(fill="x", pady=(0, 8))

        self._error = ctk.CTkLabel(body, text="", text_color=COLORS["danger"], font=font(12), anchor="w")
        self._error.pack(fill="x")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 18))

        ctk.CTkButton(
            btn_frame,
            text="取消",
            width=84,
            command=self.destroy,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_frame,
            text="确定",
            width=84,
            command=self._confirm,
            **button_style("primary"),
        ).pack(side="right")

        self.bind("<Return>", lambda _event: self._confirm())
        center_window(self, master)

    def _confirm(self):
        password = self._password.get()
        if len(password) < 8:
            self._error.configure(text="迁移密码至少需要 8 个字符")
            return
        if self._confirm_password and self._password_confirm:
            if password != self._password_confirm.get():
                self._error.configure(text="两次输入的迁移密码不一致")
                return

        self.destroy()
        self._on_confirm(password)
