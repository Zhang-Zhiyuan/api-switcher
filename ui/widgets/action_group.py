"""Wrap existing action groups without replacing widgets or their callbacks."""
from __future__ import annotations

import customtkinter as ctk

from ui.theme import bind_wraplength, button_style


def wrap_action_group(container, *, hint=None, wide_columns=3, advanced=()):
    """Turn a packed sidebar into a bounded column group when it gets a full row.

    Labels delimit groups; controls retain their order, enabled state and command.
    The container must be width-constrained by its parent (e.g. sticky="ew").
    """
    widgets = tuple(container.pack_slaves())
    secondary = set(advanced)
    # A heading belongs to the folded section when all its buttons do.
    for index, widget in enumerate(widgets):
        if not isinstance(widget, ctk.CTkLabel) or widget is hint:
            continue
        following = []
        for member in widgets[index + 1:]:
            if isinstance(member, ctk.CTkLabel):
                break
            following.append(member)
        if following and all(member in secondary for member in following):
            secondary.add(widget)
    toggle = (ctk.CTkButton(container, text="高级检测 ▾", width=118,
                           **button_style("secondary", compact=True)) if secondary else None)
    expanded = False
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
        columns = wide_columns if width >= 520 else 2 if width >= 300 else 1
        signature = (columns, expanded, container._get_widget_scaling())
        if signature == columns_before:
            return
        columns_before = signature
        if toggle is not None:
            shown = [widget for widget in widgets if widget not in secondary] + [toggle]
            if expanded:
                shown += [widget for widget in widgets if widget in secondary]
            for widget in secondary:
                if not expanded:
                    widget.grid_forget()
        else:
            shown = widgets
        for col in range(max(3, wide_columns)):
            container.grid_columnconfigure(col, weight=1 if col < columns else 0,
                                           uniform="action-group" if col < columns else "")
        row = col = 0
        for widget in shown:
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

    def toggle_advanced():
        nonlocal expanded
        expanded = not expanded
        toggle.configure(text="收起高级检测 ▴" if expanded else "高级检测 ▾")
        layout()

    if toggle is not None:
        toggle.configure(command=toggle_advanced)
    container.bind("<Configure>", layout, add="+")
    layout()
    return toggle
