import customtkinter as ctk

from ui.theme import COLORS, button_style, center_window, font


class BulkOperationResultDialog(ctk.CTkToplevel):
    """Show bulk operation summary with per-item success/failure details."""

    def __init__(self, master, title: str, success_count: int, failure_items: list[str], success_label: str):
        super().__init__(master)
        self.title(title)
        self.geometry("680x460")
        self.resizable(True, True)
        self.minsize(560, 360)
        self.configure(fg_color=COLORS["app_bg"])
        self.grab_set()

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 8))
        ctk.CTkLabel(header, text=title, text_color=COLORS["text"], font=font(18, "bold")).pack(anchor="w")

        summary = ctk.CTkFrame(self, fg_color="transparent")
        summary.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(
            summary,
            text=f"成功: {success_count} 个  |  失败: {len(failure_items)} 个",
            text_color=COLORS["text"],
            font=font(13, "bold"),
        ).pack(anchor="w")
        ctk.CTkLabel(
            summary,
            text=success_label,
            text_color=COLORS["muted"],
            font=font(12),
        ).pack(anchor="w", pady=(4, 0))

        body = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS["surface"],
            scrollbar_button_color=COLORS["secondary"],
            scrollbar_button_hover_color=COLORS["secondary_hover"],
        )
        body.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        if failure_items:
            ctk.CTkLabel(body, text="失败详情", text_color=COLORS["danger"], font=font(14, "bold")).pack(anchor="w", pady=(4, 8))
            for item in failure_items:
                row = ctk.CTkFrame(body, fg_color="transparent")
                row.pack(fill="x", pady=4)
                ctk.CTkLabel(
                    row,
                    text=f"• {item}",
                    text_color=COLORS["text"],
                    justify="left",
                    wraplength=620,
                    anchor="w",
                    font=font(12),
                ).pack(fill="x")
        else:
            ctk.CTkLabel(body, text="没有失败项。", text_color=COLORS["success"], font=font(13, "bold")).pack(anchor="w", pady=8)

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkButton(btn_frame, text="关闭", width=84, command=self.destroy, **button_style("primary")).pack(side="right")

        center_window(self, master)
