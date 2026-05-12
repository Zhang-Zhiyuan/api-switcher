import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, center_window, font


class CloseChoiceDialog(ctk.CTkToplevel):
    """Ask whether closing should exit the app or hide it to the tray."""

    def __init__(self, master, on_minimize=None, on_exit=None, on_cancel=None):
        super().__init__(master)
        self.title("关闭 API切换器")
        self.geometry("460x220")
        self.minsize(420, 200)
        self.resizable(False, False)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        self._on_minimize = on_minimize
        self._on_exit = on_exit
        self._on_cancel = on_cancel
        self._finished = False

        self.protocol("WM_DELETE_WINDOW", self._cancel)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(18, 12))

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

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 18))

        ctk.CTkButton(
            btn_frame,
            text="取消",
            width=78,
            command=self._cancel,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_frame,
            text="直接退出",
            width=92,
            command=self._exit,
            **button_style("danger"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            btn_frame,
            text="最小化到托盘",
            width=118,
            command=self._minimize,
            **button_style("primary"),
        ).pack(side="right")

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
