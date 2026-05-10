import customtkinter as ctk

from core.switch_preview import SwitchPreview, build_switch_preview
from ui.theme import COLORS, button_style, center_window, font, textbox_style


class SwitchPreviewDialog(ctk.CTkToplevel):
    """Modal preview shown before a profile switch mutates local config files."""

    def __init__(self, master, preview: SwitchPreview, on_confirm=None, on_cancel=None):
        super().__init__(master)
        self.preview = preview
        self._on_confirm = on_confirm
        self._on_cancel = on_cancel
        self._closed_by_confirm = False

        self.title(preview.title)
        self.geometry("760x620")
        self.minsize(680, 520)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._build_ui()
        center_window(self, master)

    def _build_ui(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(18, 12))

        ctk.CTkLabel(
            body,
            text=self.preview.title,
            text_color=COLORS["text"],
            font=font(18, "bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            body,
            text=self.preview.summary,
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        ).pack(fill="x", anchor="w", pady=(4, 10))

        status_frame = ctk.CTkFrame(body, fg_color=COLORS["surface"], corner_radius=8)
        status_frame.pack(fill="x", pady=(0, 10))
        status_text = "可切换"
        status_color = COLORS["success"]
        if self.preview.error_count:
            status_text = f"发现 {self.preview.error_count} 个阻断问题"
            status_color = COLORS["danger"]
        elif self.preview.warning_count:
            status_text = f"有 {self.preview.warning_count} 个提醒"
            status_color = COLORS["warning"]

        ctk.CTkLabel(
            status_frame,
            text=status_text,
            text_color=status_color,
            font=font(13, "bold"),
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkLabel(
            status_frame,
            text="确认后会先创建备份，再写入本机配置文件。",
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(side="left", padx=(8, 12), pady=8)

        self.textbox = ctk.CTkTextbox(body, wrap="word", **textbox_style(monospace=True))
        self.textbox.pack(fill="both", expand=True)
        self._write_preview_text()
        self.textbox.configure(state="disabled")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=(0, 18))

        ctk.CTkButton(
            btn_frame,
            text="取消",
            width=96,
            command=self._cancel,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))

        confirm_button = ctk.CTkButton(
            btn_frame,
            text="确认切换",
            width=110,
            command=self._confirm,
            state="normal" if self.preview.can_proceed else "disabled",
            **button_style("primary"),
        )
        confirm_button.pack(side="right")

    def _write_preview_text(self):
        self._insert("将要变更\n")
        self._insert("-" * 72 + "\n")
        for change in self.preview.changes:
            flag = "!" if change.important else " "
            self._insert(f"{flag} {change.label}\n")
            self._insert(f"  当前: {change.before}\n")
            self._insert(f"  切换后: {change.after}\n")
            if change.note:
                self._insert(f"  备注: {change.note}\n")
            self._insert("\n")

        if self.preview.files:
            self._insert("写入文件\n")
            self._insert("-" * 72 + "\n")
            for path in self.preview.files:
                self._insert(f"- {path}\n")
            self._insert("\n")

        self._insert("切换前健康检查\n")
        self._insert("-" * 72 + "\n")
        if not self.preview.checks:
            self._insert("[OK] 没有发现阻断问题。\n")
        for check in self.preview.checks:
            marker = {"ok": "OK", "warning": "WARN", "error": "ERROR"}.get(check.status, check.status.upper())
            self._insert(f"[{marker}] {check.category} / {check.item}: {check.message}\n")
            if check.suggestion:
                self._insert(f"  建议: {check.suggestion}\n")
        if self.preview.error_count:
            self._insert("\n存在阻断问题，修复后再切换。\n")

    def _insert(self, text: str):
        self.textbox.insert("end", text)

    def _confirm(self):
        self._closed_by_confirm = True
        if self._on_confirm:
            self._on_confirm()
        self.destroy()

    def _cancel(self):
        if not self._closed_by_confirm and self._on_cancel:
            self._on_cancel()
        self.destroy()


def show_switch_preview(master, kind: str, name: str, on_confirm=None, on_cancel=None) -> SwitchPreviewDialog:
    preview = build_switch_preview(kind, name)
    return SwitchPreviewDialog(master, preview, on_confirm=on_confirm, on_cancel=on_cancel)
