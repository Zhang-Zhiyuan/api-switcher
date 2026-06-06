import customtkinter as ctk

from ui.theme import COLORS, font


class Toast(ctk.CTkToplevel):
    """A brief notification popup that auto-dismisses."""

    def __init__(self, master, message: str, duration: int = 2000, is_error: bool = False):
        super().__init__(master)
        self._dismiss_after_id = None
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        accent = COLORS["danger"] if is_error else COLORS["success"]
        self.configure(fg_color=COLORS["surface"])

        body = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            border_width=1,
            border_color=accent,
            corner_radius=8,
        )
        body.pack()

        label = ctk.CTkLabel(
            body,
            text=message,
            text_color=COLORS["text"],
            font=font(13),
            padx=20,
            pady=11,
            wraplength=360,
        )
        label.pack()

        # Position near top-right of parent, clamped to the visible screen.
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        try:
            screen_x = master.winfo_vrootx()
            screen_y = master.winfo_vrooty()
            screen_width = master.winfo_vrootwidth()
            screen_height = master.winfo_vrootheight()
        except Exception:
            screen_x = 0
            screen_y = 0
            screen_width = master.winfo_screenwidth()
            screen_height = master.winfo_screenheight()

        px = master.winfo_rootx() + max(master.winfo_width(), 1) - width - 20
        py = master.winfo_rooty() + 40
        px = min(max(px, screen_x + 8), screen_x + max(screen_width - width - 8, 0))
        py = min(max(py, screen_y + 8), screen_y + max(screen_height - height - 8, 0))
        self.geometry(f"+{px}+{py}")

        self._dismiss_after_id = self.after(duration, self._dismiss)

    def _dismiss(self):
        self._dismiss_after_id = None
        self.destroy()

    def destroy(self):
        if self._dismiss_after_id:
            try:
                self.after_cancel(self._dismiss_after_id)
            except Exception:
                pass
            self._dismiss_after_id = None
        super().destroy()


def show_toast(master, message: str, is_error: bool = False):
    """Show a toast notification."""
    Toast(master, message, is_error=is_error)
