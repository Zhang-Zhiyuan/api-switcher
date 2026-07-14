from __future__ import annotations

import customtkinter as ctk

from ui.theme import COLORS, font


def adaptive_tab_columns(width: int, item_count: int, item_min_width: int = 108) -> int:
    """Return a stable number of columns for a wrapping tab bar."""

    count = max(1, int(item_count))
    available = max(1, int(width))
    item_width = max(1, int(item_min_width))
    return min(count, max(1, available // item_width))


class AdaptiveTabBar(ctk.CTkFrame):
    """A tab selector that wraps instead of clipping labels on narrow screens."""

    def __init__(self, master, values: list[str], command=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._values = list(values)
        self._command = command
        self._selected = ""
        self._enabled = True
        self._buttons: dict[str, ctk.CTkButton] = {}
        self._column_count = 0
        self._layout_after_id = None

        for value in self._values:
            button = ctk.CTkButton(
                self,
                text=value,
                width=0,
                height=30,
                corner_radius=6,
                border_width=1,
                font=font(11, "bold"),
                fg_color=COLORS["surface_alt"],
                hover_color=COLORS["surface_hover"],
                border_color=COLORS["border_soft"],
                text_color=COLORS["text"],
                command=lambda selected=value: self._select_from_user(selected),
            )
            self._buttons[value] = button

        self.bind("<Configure>", self._schedule_layout, add="+")
        self.after_idle(self._layout_buttons)
        if self._values:
            self.set(self._values[0])

    def get(self) -> str:
        return self._selected

    def set(self, value: str) -> None:
        if value not in self._buttons or value == self._selected:
            return
        previous = self._selected
        self._selected = value
        if previous in self._buttons:
            self._configure_button(previous, selected=False)
        self._configure_button(value, selected=True)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        state = "normal" if self._enabled else "disabled"
        for button in self._buttons.values():
            button.configure(state=state)

    def _configure_button(self, value: str, *, selected: bool) -> None:
        self._buttons[value].configure(
            fg_color=COLORS["primary"] if selected else COLORS["surface_alt"],
            hover_color=COLORS["primary_hover"] if selected else COLORS["surface_hover"],
            border_color=COLORS["primary"] if selected else COLORS["border_soft"],
            text_color=COLORS["text"],
        )

    def _select_from_user(self, value: str) -> None:
        if not self._enabled:
            return
        self.set(value)
        if self._command is not None:
            self._command(value)

    def _logical_width(self) -> int:
        width = self.winfo_width()
        try:
            scaling = float(self._get_widget_scaling())
        except (AttributeError, TypeError, ValueError):
            scaling = 1.0
        if scaling > 0:
            width = round(width / scaling)
        return max(1, width)

    def _schedule_layout(self, _event=None) -> None:
        if self._layout_after_id is not None:
            return
        try:
            self._layout_after_id = self.after_idle(self._layout_buttons)
        except Exception:
            self._layout_after_id = None

    def _layout_buttons(self) -> None:
        self._layout_after_id = None
        if not self._buttons:
            return
        columns = adaptive_tab_columns(self._logical_width(), len(self._values))
        if columns == self._column_count:
            return

        previous_columns = self._column_count
        self._column_count = columns
        for column in range(max(previous_columns, columns)):
            self.grid_columnconfigure(column, weight=0, minsize=0, uniform="")
        for column in range(columns):
            self.grid_columnconfigure(column, weight=1, uniform="adaptive-tabs")

        for index, value in enumerate(self._values):
            self._buttons[value].grid(
                row=index // columns,
                column=index % columns,
                sticky="ew",
                padx=2,
                pady=2,
            )
