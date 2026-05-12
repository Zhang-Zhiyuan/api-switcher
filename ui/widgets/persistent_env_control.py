import customtkinter as ctk

from core import persistent_env
from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, combo_style, font
from ui.widgets.masked_entry import MaskedEntry
from ui.widgets.toast import show_toast


class PersistentEnvControl(ctk.CTkFrame):
    """Reusable control for writing, deleting, and importing persistent env vars."""

    def __init__(
        self,
        master,
        title: str | None = None,
        status_text: str = "",
        write_label: str = "写入",
        delete_label: str = "删除",
        on_write=None,
        on_delete=None,
        **kwargs,
    ):
        super().__init__(master, **card_frame_kwargs(), **kwargs)
        self._sources = []
        self._on_write = on_write
        self._on_delete = on_delete

        if title:
            ctk.CTkLabel(
                self,
                text=title,
                text_color=COLORS["text"],
                font=font(14, "bold"),
            ).pack(anchor="w", padx=14, pady=(12, 8))

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(fill="x", padx=14, pady=(0, 8) if title else 14)
        controls.grid_columnconfigure(0, weight=1)

        field_grid = ctk.CTkFrame(controls, fg_color="transparent")
        field_grid.grid(row=0, column=0, sticky="ew")
        field_grid.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            field_grid,
            text="变量名",
            text_color=COLORS["muted"],
            width=64,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.name_combo = ctk.CTkComboBox(
            field_grid,
            values=persistent_env.COMMON_ENV_NAMES,
            width=220,
            **combo_style(),
        )
        self.name_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.name_combo.set("HF_TOKEN")

        ctk.CTkLabel(
            field_grid,
            text="值",
            text_color=COLORS["muted"],
            width=64,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.value_entry = MaskedEntry(field_grid, placeholder="Token / value", width=360)
        self.value_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(10, 0))

        action_row = ctk.CTkFrame(controls, fg_color="transparent")
        action_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))

        ctk.CTkButton(
            action_row,
            text=write_label,
            width=124,
            command=self._write,
            **button_style("primary"),
        ).pack(side="right")
        ctk.CTkButton(
            action_row,
            text=delete_label,
            width=124,
            command=self._delete,
            **button_style("danger"),
        ).pack(side="right", padx=(0, 8))
        ctk.CTkButton(
            action_row,
            text="清空值",
            width=84,
            command=self.clear_value,
            **button_style("secondary"),
        ).pack(side="right", padx=(0, 8))

        separator = ctk.CTkFrame(controls, height=1, fg_color=COLORS["border_soft"])
        separator.grid(row=2, column=0, sticky="ew", pady=(12, 10))

        import_grid = ctk.CTkFrame(controls, fg_color="transparent")
        import_grid.grid(row=3, column=0, sticky="ew")
        import_grid.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            import_grid,
            text="导入来源",
            text_color=COLORS["muted"],
            width=64,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.source_combo = ctk.CTkComboBox(
            import_grid,
            values=["(暂无可导入来源)"],
            width=360,
            **combo_style(),
        )
        self.source_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        import_actions = ctk.CTkFrame(import_grid, fg_color="transparent")
        import_actions.grid(row=1, column=1, sticky="e", pady=(10, 0))

        ctk.CTkButton(
            import_actions,
            text="刷新来源",
            width=106,
            command=self.refresh_sources,
            **button_style("secondary"),
        ).pack(side="right")
        ctk.CTkButton(
            import_actions,
            text="填入",
            width=78,
            command=self.import_selected_source,
            **button_style("secondary"),
        ).pack(side="right", padx=(0, 8))

        self.status_label = ctk.CTkLabel(
            controls,
            text=status_text,
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        self.status_label.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        bind_wraplength(controls, self.status_label, padding=20)

        self.refresh_sources()

    def env_name(self) -> str:
        return self.name_combo.get()

    def env_value(self) -> str:
        return self.value_entry.get()

    def env_update(self) -> dict[str, str]:
        return persistent_env.normalize_env_updates({self.env_name(): self.env_value()})

    def env_names(self) -> list[str]:
        return persistent_env.normalize_env_names(self.env_name())

    def set_status(self, message: str, severity: str = "info") -> None:
        color = {
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["danger"],
        }.get(severity, COLORS["muted"])
        self.status_label.configure(text=message, text_color=color)

    def clear_value(self) -> None:
        self.value_entry.set("")
        self.set_status("已清空当前值，变量名保留不变。")

    def refresh_sources(self) -> None:
        current = self.source_combo.get()
        self._sources = persistent_env.list_env_import_sources()
        labels = [source.display_label() for source in self._sources]
        self.source_combo.configure(values=labels if labels else ["(暂无可导入来源)"])
        if labels:
            self.source_combo.set(current if current in labels else labels[0])
        else:
            self.source_combo.set("(暂无可导入来源)")

    def import_selected_source(self) -> None:
        label = self.source_combo.get()
        source = next((item for item in self._sources if item.display_label() == label), None)
        if not source:
            show_toast(self.winfo_toplevel(), "暂无可导入来源", is_error=True)
            return
        self.name_combo.set(source.env_name)
        self.value_entry.set(source.value)
        self.set_status(
            f"已填入 {source.env_name}，来源: {source.label}，值: {source.preview_value()}",
            "success",
        )
        show_toast(self.winfo_toplevel(), f"已填入 {source.env_name}")

    def _write(self) -> None:
        if self._on_write:
            self._on_write(self)

    def _delete(self) -> None:
        if self._on_delete:
            self._on_delete(self)
