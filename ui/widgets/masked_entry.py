import customtkinter as ctk

from ui.theme import button_style, input_style


class MaskedEntry(ctk.CTkFrame):
    """An entry widget with a show/hide toggle for sensitive text."""

    def __init__(self, master, placeholder="API Key / Token", **kwargs):
        super().__init__(master, fg_color="transparent")

        entry_width = kwargs.pop("width", 400)
        entry_height = kwargs.pop("height", 34)
        style = input_style()
        style.pop("height", None)
        style.update(kwargs)
        self.entry = ctk.CTkEntry(
            self,
            placeholder_text=placeholder,
            show="*",
            width=entry_width,
            height=entry_height,
            **style,
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self._visible = False
        self.toggle_btn = ctk.CTkButton(
            self,
            text="显示",
            width=58,
            command=self._toggle,
            **button_style("secondary", compact=True),
        )
        self.toggle_btn.pack(side="right")

    def _toggle(self):
        self._visible = not self._visible
        self.entry.configure(show="" if self._visible else "*")
        self.toggle_btn.configure(text="隐藏" if self._visible else "显示")

    def get(self) -> str:
        return self.entry.get()

    def set(self, value: str):
        self.entry.delete(0, "end")
        self.entry.insert(0, value)
