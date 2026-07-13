import customtkinter as ctk

from ui.theme import COLORS, _screen_bounds, _window_scaling, font


def _toast_wraplength(screen_width: int, scaling: float = 1.0) -> int:
    """Return a logical text width that leaves room for padding and screen edges."""

    try:
        scale = max(float(scaling), 0.1)
    except (TypeError, ValueError):
        scale = 1.0
    logical_screen_width = max(1, round(max(1, int(screen_width)) / scale))
    return max(1, min(360, logical_screen_width - 64))


class Toast(ctk.CTkToplevel):
    """A brief notification popup that auto-dismisses."""

    def __init__(self, master, message: str, duration: int = 2000, is_error: bool = False):
        super().__init__(master)
        self._dismiss_after_id = None
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        accent = COLORS["danger"] if is_error else COLORS["success"]
        self.configure(fg_color=COLORS["surface"])
        screen_x, screen_y, screen_width, screen_height = _screen_bounds(master)
        wraplength = _toast_wraplength(screen_width, _window_scaling(self))

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
            wraplength=wraplength,
        )
        label.pack()

        # Position near top-right of parent, clamped to the visible screen.
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
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
