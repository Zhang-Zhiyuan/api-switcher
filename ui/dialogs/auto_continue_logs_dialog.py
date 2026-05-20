import customtkinter as ctk

from core.auto_continue.diagnostics import format_auto_continue_diagnostics
from ui.theme import COLORS, button_style, center_window, combo_style, font, textbox_style
from ui.widgets.toast import show_toast


class AutoContinueLogsDialog(ctk.CTkToplevel):
    """Recent auto-continue decision and recovery logs."""

    def __init__(self, master, provider: str):
        super().__init__(master)
        self.provider = provider
        self.title(f"{provider} 自动续跑日志")
        self.geometry("980x720")
        self.minsize(780, 560)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()

        self._build_ui()
        center_window(self, master)
        self._refresh()

    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(18, 10))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            title_area,
            text="自动续跑日志",
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_area,
            text="最近 Stop 决策、API 恢复、命中规则、次数和训练模板。",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(2, 0))

        ctk.CTkButton(
            header,
            text="复制诊断",
            width=104,
            command=self._copy_diagnostics,
            **button_style("accent"),
        ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(
            header,
            text="刷新",
            width=82,
            command=self._refresh,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(controls, text="最近条数", text_color=COLORS["muted"], font=font(12)).pack(side="left")
        self._limit_combo = ctk.CTkComboBox(
            controls,
            values=["50", "100", "200", "500"],
            width=90,
            command=lambda _value: self._refresh(),
            **combo_style(),
        )
        self._limit_combo.set("100")
        self._limit_combo.pack(side="left", padx=(8, 16))

        self._status_label = ctk.CTkLabel(
            controls,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
        )
        self._status_label.pack(side="left", fill="x", expand=True)

        self._text = ctk.CTkTextbox(self, wrap="none", **textbox_style(monospace=True))
        self._text.pack(fill="both", expand=True, padx=18, pady=(0, 18))

    def _limit(self) -> int:
        try:
            return int(self._limit_combo.get())
        except Exception:
            return 100

    def _diagnostics_text(self) -> str:
        return format_auto_continue_diagnostics(self.provider, self._limit())

    def _refresh(self):
        try:
            text = self._diagnostics_text()
            self._text.configure(state="normal")
            self._text.delete("1.0", "end")
            self._text.insert("1.0", text)
            self._text.configure(state="disabled")
            self._status_label.configure(text=f"{self.provider} 日志已刷新", text_color=COLORS["muted"])
        except Exception as e:
            self._text.configure(state="normal")
            self._text.delete("1.0", "end")
            self._text.insert("1.0", f"读取失败: {e}")
            self._text.configure(state="disabled")
            self._status_label.configure(text="读取失败", text_color=COLORS["danger"])

    def _copy_diagnostics(self):
        text = self._text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(text)
        show_toast(self, "自动续跑诊断信息已复制")
