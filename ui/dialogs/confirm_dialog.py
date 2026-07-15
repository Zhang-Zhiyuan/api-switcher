import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, center_window, font


def _dialog_action_columns(width: int) -> int:
    """Stack paired actions only when two readable buttons no longer fit."""

    return 2 if width >= 220 else 1


def _bind_two_button_footer(frame, primary_button, secondary_button) -> None:
    state = {"columns": 0}
    buttons = (primary_button, secondary_button)

    def apply_layout(event=None):
        try:
            width = int(getattr(event, "width", 0) or frame.winfo_width())
            columns = _dialog_action_columns(width)
            if columns == state["columns"]:
                return
            previous = state["columns"]
            state["columns"] = columns
            for column in range(max(previous, columns)):
                frame.grid_columnconfigure(column, weight=1 if column < columns else 0, minsize=0)
            for index, button in enumerate(buttons):
                button.grid(
                    row=index // columns,
                    column=index % columns,
                    sticky="ew",
                    padx=(0 if index % columns == 0 else 4, 0),
                    pady=(0 if index == 0 else 5, 0) if columns == 1 else 0,
                )
        except Exception:
            return

    frame.bind("<Configure>", apply_layout, add="+")
    apply_layout()


class ConfirmDialog(ctk.CTkToplevel):
    """A simple confirmation dialog with Yes/No buttons."""

    def __init__(self, master, title="确认", message="确定要执行此操作吗？", on_confirm=None):
        super().__init__(master)
        self.title(title)
        self.geometry("440x210")
        self.minsize(380, 180)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_confirm = on_confirm

        # Reserve the footer before the body.  When the monitor is extremely
        # short, Tk's packer can then shrink the scrollable body without
        # pushing the action buttons below the visible work area.
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
            font=font(13),
        )
        message_label.pack(fill="x", anchor="w")
        bind_wraplength(body, message_label, padding=4, min_width=240, max_width=560)

        cancel_button = ctk.CTkButton(
            btn_frame,
            text="取消",
            width=1,
            command=self.destroy,
            **button_style("secondary"),
        )
        confirm_button = ctk.CTkButton(
            btn_frame,
            text="确定",
            width=1,
            command=self._confirm,
            **button_style("danger"),
        )
        _bind_two_button_footer(btn_frame, confirm_button, cancel_button)

        center_window(self, master)

    def _confirm(self):
        if self._on_confirm:
            self._on_confirm()
        self.destroy()
