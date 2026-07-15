import customtkinter as ctk

from ui.dialogs.confirm_dialog import _bind_two_button_footer
from ui.theme import COLORS, bind_wraplength, button_style, center_window, font, input_style


class DangerConfirmDialog(ctk.CTkToplevel):
    """Require typing the exact profile name before a dangerous operation."""

    def __init__(self, master, title: str, message: str, confirm_text: str, on_confirm=None):
        super().__init__(master)
        self.title(title)
        self.geometry("560x280")
        self.minsize(460, 250)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._confirm_text = confirm_text
        self._on_confirm = on_confirm

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(side="bottom", fill="x", padx=18, pady=(6, 16))

        body = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
        )
        body.pack(fill="both", expand=True, padx=12, pady=(10, 0))

        ctk.CTkLabel(
            body,
            text=title,
            text_color=COLORS["danger"],
            font=font(18, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        message_label = ctk.CTkLabel(
            body,
            text=message,
            text_color=COLORS["text"],
            font=font(12),
            justify="left",
            anchor="w",
        )
        message_label.pack(fill="x", pady=(0, 12))
        bind_wraplength(body, message_label, padding=4, min_width=320, max_width=680)

        ctk.CTkLabel(
            body,
            text=f'请输入名称确认: {confirm_text}',
            text_color=COLORS["muted"],
            font=font(12, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        self._entry = ctk.CTkEntry(body, width=1, **input_style())
        self._entry.pack(fill="x")

        self._error = ctk.CTkLabel(body, text="", text_color=COLORS["danger"], font=font(11))
        self._error.pack(fill="x", pady=(6, 0))

        cancel_button = ctk.CTkButton(
            btn_frame,
            text="取消",
            width=1,
            command=self.destroy,
            **button_style("secondary"),
        )
        confirm_button = ctk.CTkButton(
            btn_frame,
            text="确认清理",
            width=1,
            command=self._submit,
            **button_style("danger"),
        )
        _bind_two_button_footer(btn_frame, confirm_button, cancel_button)

        center_window(self, master)

    def _submit(self):
        if self._entry.get().strip() != self._confirm_text:
            self._error.configure(text="输入的名称不匹配，已取消操作")
            return
        if self._on_confirm:
            self._on_confirm()
        self.destroy()
