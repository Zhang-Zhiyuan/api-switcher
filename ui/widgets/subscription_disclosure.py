"""Keep subscription editing out of the everyday node-selection workflow."""
from __future__ import annotations

import customtkinter as ctk

from ui.theme import button_style


class SubscriptionDisclosure:
    """Hide existing form widgets without replacing their values or callbacks.

    The owning page still controls field layout and operation state. Call
    ``reflow`` after its responsive layout and ``set_context`` after loading or
    editing a subscription. Switching to a new subscription reveals its form.
    """

    def __init__(self, actions, *, fields, items, refresh):
        self.actions = actions
        self.fields = tuple(fields)
        self.items = tuple(items)
        self.refresh = refresh
        self.expanded = True
        self._profile_id = None
        self._dirty = False
        self._layout_signature = None
        self._field_layouts = {}
        self.toggle = ctk.CTkButton(
            actions, text="收起订阅编辑 ▴", width=138, command=self._toggle,
            **button_style("secondary", compact=True),
        )
        actions.bind("<Configure>", self._on_resize, add="+")
        self.reflow()

    def set_context(self, profile_id, *, dirty=False, busy=False):
        profile_id = str(profile_id or "")
        if profile_id != self._profile_id:
            self.expanded = not profile_id
            self._profile_id = profile_id
        self._dirty = bool(dirty)
        state = "disabled" if busy else "normal"
        if self.toggle.cget("state") != state:
            self.toggle.configure(state=state)
        self.reflow()

    def _toggle(self):
        self.expanded = not self.expanded
        self.reflow()

    def _on_resize(self, _event=None):
        self._layout_actions()

    def reflow(self):
        for widget in self.fields:
            if self.expanded:
                if not widget.winfo_manager():
                    widget.grid(**self._field_layouts[widget])
            elif widget.winfo_manager():
                # grid_remove leaves CTk's geometry replay active, so a DPI
                # change would reveal the hidden fields. Keep logical units.
                self._field_layouts[widget] = dict(widget._last_geometry_manager_call["kwargs"])
                widget.grid_forget()
        label = "收起订阅编辑 ▴" if self.expanded else "编辑订阅 ▾"
        if self._dirty:
            label = "收起编辑 · 未保存 ▴" if self.expanded else "编辑订阅 · 未保存 ▾"
        if self.toggle.cget("text") != label:
            self.toggle.configure(text=label)
        self._layout_actions()

    def _layout_actions(self):
        width = self.actions.winfo_width() / self.actions._get_widget_scaling()
        columns = 4 if width >= 760 else 3 if width >= 560 else 2 if width >= 340 else 1
        shown = (*self.items, self.toggle) if self.expanded else (self.refresh, self.toggle)
        columns = min(columns, len(shown))
        signature = (columns, self.expanded, self.actions._get_widget_scaling())
        if signature == self._layout_signature:
            return
        self._layout_signature = signature
        for widget in (*self.items, self.toggle):
            if widget not in shown:
                widget.grid_forget()
        for column in range(4):
            self.actions.grid_columnconfigure(
                column, weight=int(self.expanded and column < columns),
                uniform="subscription-actions" if self.expanded and column < columns else "",
            )
        for index, widget in enumerate(shown):
            widget.grid(row=index // columns, column=index % columns, columnspan=1, sticky="ew",
                        padx=(0 if index % columns == 0 else 8, 0), pady=(0 if index < columns else 6, 0))
