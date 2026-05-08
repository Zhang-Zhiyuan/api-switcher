import customtkinter as ctk

from ui.theme import COLORS, font


class Toast(ctk.CTkToplevel):
    """A brief notification popup that auto-dismisses."""

    def __init__(self, master, message: str, duration: int = 2000, is_error: bool = False):
        super().__init__(master)
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

        # Position near top-right of parent
        self.update_idletasks()
        px = master.winfo_rootx() + master.winfo_width() - self.winfo_width() - 20
        py = master.winfo_rooty() + 40
        self.geometry(f"+{px}+{py}")

        self.after(duration, self.destroy)


def show_toast(master, message: str, is_error: bool = False):
    """Show a toast notification."""
    Toast(master, message, is_error=is_error)
