import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, center_window, font


class ConfirmDialog(ctk.CTkToplevel):
    """A simple confirmation dialog with Yes/No buttons."""

    def __init__(self, master, title="确认", message="确定要执行此操作吗？", on_confirm=None):
        super().__init__(master)
        self.title(title)
        self.geometry("440x210")
        self.minsize(380, 180)
        self.resizable(True, False)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_confirm = on_confirm

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
            font=font(13),
        )
        message_label.pack(fill="x", anchor="w")
        bind_wraplength(body, message_label, padding=4, min_width=240, max_width=560)

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
            **button_style("danger"),
        ).pack(side="right")

        center_window(self, master)

    def _confirm(self):
        if self._on_confirm:
            self._on_confirm()
        self.destroy()
