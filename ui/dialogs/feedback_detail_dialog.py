"""Scrollable details for notifications that would otherwise cover the app."""
import customtkinter as ctk

from ui.feedback import feedback_title, safe_feedback_text
from ui.theme import COLORS, button_style, center_window, font


class FeedbackDetailDialog(ctk.CTkToplevel):
    def __init__(self, master, message: str, severity: str = "info"):
        super().__init__(master)
        self._message = safe_feedback_text(message)
        self.title("操作详情")
        self.geometry("760x460")
        self.minsize(320, 240)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self, text=feedback_title(severity), font=font(18, "bold"), anchor="w").grid(
            row=0, column=0, sticky="ew", padx=20, pady=(16, 8),
        )
        self._text = ctk.CTkTextbox(self, wrap="word", font=font(13))
        self._text.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 12))
        self._text.insert("1.0", self._message)
        self._text.configure(state="disabled")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 16))
        self._copy_button = ctk.CTkButton(actions, text="复制详情", command=self._copy, **button_style("secondary"))
        self._copy_button.pack(side="left")
        ctk.CTkButton(actions, text="关闭", command=self.destroy, **button_style("primary")).pack(side="right")
        self.bind("<Escape>", lambda _event: self.destroy())
        center_window(self, master)

    def _copy(self):
        self.clipboard_clear()
        self.clipboard_append(self._message)
        self._copy_button.configure(text="已复制")
