import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, center_window, font


def _close_choice_button_columns(width: int) -> int:
    """Return a button layout that remains usable on narrow monitors."""

    if width < 180:
        return 1
    return 3 if width >= 380 else 2


class CloseChoiceDialog(ctk.CTkToplevel):
    """Ask whether closing should exit the app or hide it to the tray."""

    def __init__(self, master, on_minimize=None, on_exit=None, on_cancel=None):
        super().__init__(master)
        self.title("关闭 API切换器")
        self.geometry("460x220")
        self.minsize(420, 200)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_minimize = on_minimize
        self._on_exit = on_exit
        self._on_cancel = on_cancel
        self._finished = False

        self.protocol("WM_DELETE_WINDOW", self._cancel)

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
            text="要关闭程序还是最小化到托盘？",
            text_color=COLORS["text"],
            font=font(16, "bold"),
        ).pack(anchor="w", pady=(0, 8))

        message_label = ctk.CTkLabel(
            body,
            text="最小化后程序会继续在系统右下角托盘运行；直接退出会停止当前 GUI 进程。",
            justify="left",
            text_color=COLORS["muted"],
            font=font(13),
        )
        message_label.pack(fill="x", anchor="w")
        bind_wraplength(body, message_label, padding=4, min_width=260, max_width=560)

        cancel_btn = ctk.CTkButton(
            btn_frame,
            text="取消",
            width=1,
            command=self._cancel,
            **button_style("secondary"),
        )
        exit_btn = ctk.CTkButton(
            btn_frame,
            text="直接退出",
            width=1,
            command=self._exit,
            **button_style("danger"),
        )
        minimize_btn = ctk.CTkButton(
            btn_frame,
            text="最小化到托盘",
            width=1,
            command=self._minimize,
            **button_style("primary"),
        )

        buttons = (minimize_btn, exit_btn, cancel_btn)
        layout_state = {"columns": None, "after_id": None}

        def apply_button_layout():
            layout_state["after_id"] = None
            try:
                width = btn_frame.winfo_width()
                columns = _close_choice_button_columns(width)
                if layout_state["columns"] == columns:
                    return
                layout_state["columns"] = columns
                for column in range(3):
                    btn_frame.grid_columnconfigure(column, weight=1 if column < columns else 0)
                if columns == 3:
                    for column, button in enumerate(buttons):
                        button.grid(
                            row=0,
                            column=column,
                            columnspan=1,
                            sticky="ew",
                            padx=(0 if column == 0 else 4, 0),
                            pady=0,
                        )
                elif columns == 2:
                    minimize_btn.grid(
                        row=0,
                        column=0,
                        columnspan=2,
                        sticky="ew",
                        pady=(0, 5),
                    )
                    exit_btn.grid(row=1, column=0, sticky="ew", padx=(0, 4))
                    cancel_btn.grid(row=1, column=1, sticky="ew")
                else:
                    for row, button in enumerate(buttons):
                        button.grid(
                            row=row,
                            column=0,
                            columnspan=1,
                            sticky="ew",
                            pady=(0, 5) if row < len(buttons) - 1 else 0,
                        )
            except Exception:
                return

        def schedule_button_layout(_event=None):
            if layout_state["after_id"] is not None:
                return
            try:
                layout_state["after_id"] = btn_frame.after_idle(apply_button_layout)
            except Exception:
                apply_button_layout()

        btn_frame.bind("<Configure>", schedule_button_layout, add="+")
        schedule_button_layout()

        center_window(self, master)

    def _finish(self, callback):
        if self._finished:
            return
        self._finished = True
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        if callback:
            callback()

    def _minimize(self):
        self._finish(self._on_minimize)

    def _exit(self):
        self._finish(self._on_exit)

    def _cancel(self):
        self._finish(self._on_cancel)
