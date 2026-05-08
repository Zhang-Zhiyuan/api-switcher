import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font


class EmptyState(ctk.CTkFrame):
    """Compact empty state used inside list areas."""

    def __init__(self, master, title: str, detail: str = "", action_text: str = "",
                 command=None):
        super().__init__(master, **card_frame_kwargs(COLORS["border_soft"]))

        ctk.CTkLabel(
            self,
            text=title,
            text_color=COLORS["text"],
            font=font(14, "bold"),
        ).pack(pady=(18, 4))

        if detail:
            detail_label = ctk.CTkLabel(
                self,
                text=detail,
                text_color=COLORS["muted"],
                font=font(12),
                justify="center",
            )
            detail_label.pack(padx=18, pady=(0, 14))
            bind_wraplength(self, detail_label, padding=48, min_width=220, max_width=620)

        if action_text and command:
            ctk.CTkButton(
                self,
                text=action_text,
                width=120,
                command=command,
                **button_style("primary"),
            ).pack(pady=(0, 18))
