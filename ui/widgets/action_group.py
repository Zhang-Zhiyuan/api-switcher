"""Wrap existing action groups without replacing widgets or their callbacks."""
from __future__ import annotations

import customtkinter as ctk

from ui.theme import bind_wraplength


def wrap_action_group(container, *, hint=None):
    """Turn a packed sidebar into up to three columns when it gets a full row.

    Labels delimit groups; controls retain their order, enabled state and command.
    The container must be width-constrained by its parent (e.g. sticky="ew").
    """
    widgets = tuple(container.pack_slaves())
    for widget in widgets:
        widget.pack_forget()
        if isinstance(widget, ctk.CTkLabel):
            widget.configure(anchor="w", justify="left")
    if hint is not None:
        hint.configure(width=1)
        bind_wraplength(container, hint, padding=4, min_width=80, max_width=1600)
    columns_before = None

    def layout(event=None):
        nonlocal columns_before
        width = (event.width if event else container.winfo_width()) / container._get_widget_scaling()
        columns = 3 if width >= 520 else 2 if width >= 300 else 1
        if columns == columns_before:
            return
        columns_before = columns
        for col in range(3):
            container.grid_columnconfigure(col, weight=1 if col < columns else 0,
                                           uniform="action-group" if col < columns else "")
        row = col = 0
        for widget in widgets:
            if not isinstance(widget, ctk.CTkButton):
                if col:
                    row, col = row + 1, 0
                widget.grid(row=row, column=0, columnspan=columns, sticky="ew", padx=0,
                            pady=(6 if row else 0, 4))
                row += 1
            else:
                widget.grid(row=row, column=col, columnspan=1, sticky="ew",
                            padx=(0, 8) if col < columns - 1 else 0, pady=(0, 6))
                col += 1
                if col == columns:
                    row, col = row + 1, 0

    container.bind("<Configure>", layout, add="+")
    layout()
