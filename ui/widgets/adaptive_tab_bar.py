from __future__ import annotations

import math

import customtkinter as ctk

from ui.theme import COLORS, combo_style, font


ADAPTIVE_TAB_MAX_BUTTON_ROWS = 2


def adaptive_tab_uses_dropdown(
    width: int,
    item_count: int = 11,
    item_min_width: int = 96,
    max_button_rows: int = ADAPTIVE_TAB_MAX_BUTTON_ROWS,
) -> bool:
    """Use a compact selector when buttons would need too many rows."""

    count = max(1, int(item_count))
    available = max(1, int(width))
    item_width = max(1, int(item_min_width))
    maximum_columns = min(count, max(1, available // item_width))
    return math.ceil(count / maximum_columns) > max(1, int(max_button_rows))


def adaptive_tab_columns(width: int, item_count: int, item_min_width: int = 96) -> int:
    """Return balanced columns for a wrapping tab bar without an orphan row."""

    count = max(1, int(item_count))
    available = max(1, int(width))
    item_width = max(1, int(item_min_width))
    maximum_columns = min(count, max(1, available // item_width))
    row_count = math.ceil(count / maximum_columns)
    return math.ceil(count / row_count)


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
        self._layout_mode = None
        self._layout_after_id = None
        self._destroyed = False

        self._selector = ctk.CTkComboBox(
            self,
            values=self._values,
            width=0,
            state="readonly",
            command=self._select_from_user,
            **combo_style(),
        )
        self._selector.grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        self._selector.grid_remove()

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
        self._schedule_layout()
        if self._values:
            self.set(self._values[0])

    def destroy(self) -> None:
        self._destroyed = True
        self._cancel_pending_layout()
        super().destroy()

    def get(self) -> str:
        return self._selected

    def set(self, value: str) -> None:
        if value not in self._buttons:
            return
        selector = getattr(self, "_selector", None)
        if selector is not None and selector.get() != value:
            selector.set(value)
        if value == self._selected:
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
        selector = getattr(self, "_selector", None)
        if selector is not None:
            selector.configure(state="readonly" if self._enabled else "disabled")

    def _configure_button(self, value: str, *, selected: bool) -> None:
        self._buttons[value].configure(
            fg_color=COLORS["primary"] if selected else COLORS["surface_alt"],
            hover_color=COLORS["primary_hover"] if selected else COLORS["surface_hover"],
            border_color=COLORS["primary"] if selected else COLORS["border_soft"],
            border_width=2 if selected else 1,
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
        if getattr(self, "_destroyed", False) or self._layout_after_id is not None:
            return
        try:
            self._layout_after_id = self.after_idle(self._layout_buttons)
        except Exception:
            self._layout_after_id = None

    def _cancel_pending_layout(self) -> None:
        after_id = self._layout_after_id
        self._layout_after_id = None
        if after_id is None:
            return
        try:
            self.after_cancel(after_id)
        except Exception:
            pass

    def _layout_buttons(self) -> None:
        self._layout_after_id = None
        if getattr(self, "_destroyed", False):
            return
        if not self._buttons:
            return
        logical_width = self._logical_width()
        mode = "dropdown" if adaptive_tab_uses_dropdown(logical_width, len(self._values)) else "buttons"
        columns = 1 if mode == "dropdown" else adaptive_tab_columns(logical_width, len(self._values))
        if mode == self._layout_mode and columns == self._column_count:
            return

        previous_columns = self._column_count
        self._layout_mode = mode
        self._column_count = columns
        for column in range(max(previous_columns, columns)):
            self.grid_columnconfigure(column, weight=0, minsize=0, uniform="")

        if mode == "dropdown":
            for button in self._buttons.values():
                button.grid_remove()
            self.grid_columnconfigure(0, weight=1)
            self._selector.grid()
            return

        self._selector.grid_remove()
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
