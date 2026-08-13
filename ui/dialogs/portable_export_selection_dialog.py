from __future__ import annotations

from collections.abc import Iterable, Mapping

import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, center_window, font


PORTABLE_PROFILE_GROUPS = (
    ("claude_profiles", "Claude API Profile"),
    ("codex_profiles", "Codex API Profile"),
    ("ssh_profiles", "SSH Profile"),
    ("browser_profiles", "浏览器 Profile"),
)


def normalize_portable_profile_options(options: Mapping[str, Iterable[str]] | None) -> dict[str, list[str]]:
    """Return supported, non-empty Profile names in stable display order."""

    source = options if isinstance(options, Mapping) else {}
    normalized: dict[str, list[str]] = {}
    for key, _label in PORTABLE_PROFILE_GROUPS:
        values = source.get(key, ())
        if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
            values = ()
        seen: set[str] = set()
        names: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value or value in seen:
                continue
            seen.add(value)
            names.append(value)
        normalized[key] = names
    return normalized


class PortableExportSelectionDialog(ctk.CTkToplevel):
    """Choose the individual Profiles included in a portable export."""

    def __init__(self, master, options: Mapping[str, Iterable[str]], on_confirm):
        super().__init__(master)
        self.title("选择要迁移的 Profile")
        self.geometry("640x620")
        self.minsize(480, 420)
        self.resizable(True, True)
        self.configure(fg_color=COLORS["app_bg"])
        self.transient(master)
        self.grab_set()

        self._on_confirm = on_confirm
        self._options = normalize_portable_profile_options(options)
        self._variables: dict[str, dict[str, ctk.BooleanVar]] = {
            key: {} for key, _label in PORTABLE_PROFILE_GROUPS
        }
        self._total_count = sum(len(names) for names in self._options.values())

        self._build_ui()
        center_window(self, master)

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(18, 10))

        ctk.CTkLabel(
            header,
            text="选择要迁移的 Profile",
            text_color=COLORS["text"],
            font=font(17, "bold"),
            anchor="w",
        ).pack(fill="x")
        description = ctk.CTkLabel(
            header,
            text=(
                "默认全部选中。浏览器 Profile 只迁移 Local State 与 Default 下的 Cookies、Local Storage、"
                "IndexedDB 等登录数据；缓存、组件模型和运行日志不会写入迁移包。"
            ),
            text_color=COLORS["muted"],
            font=font(12),
            anchor="w",
            justify="left",
        )
        description.pack(fill="x", pady=(4, 0))
        bind_wraplength(header, description, padding=4, min_width=260, max_width=760)

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkButton(
            actions,
            text="全选",
            width=72,
            command=lambda: self._set_all(True),
            **button_style("secondary", compact=True),
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text="清空",
            width=72,
            command=lambda: self._set_all(False),
            **button_style("secondary", compact=True),
        ).pack(side="left", padx=(8, 0))
        self._status = ctk.CTkLabel(
            actions,
            text="",
            text_color=COLORS["muted"],
            font=font(12),
            anchor="e",
        )
        self._status.pack(side="right", fill="x", expand=True, padx=(12, 0))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=20, pady=(10, 18))
        self._error = ctk.CTkLabel(
            footer,
            text="",
            text_color=COLORS["danger"],
            font=font(12),
            anchor="w",
        )
        self._error.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            footer,
            text="取消",
            width=88,
            command=self.destroy,
            **button_style("secondary"),
        ).pack(side="right", padx=(8, 0))
        self._confirm_button = ctk.CTkButton(
            footer,
            text="下一步",
            width=96,
            command=self._confirm,
            **button_style("primary"),
        )
        self._confirm_button.pack(side="right")

        list_frame = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=8,
            border_width=1,
            border_color=COLORS["border_soft"],
        )
        list_frame.pack(fill="both", expand=True, padx=20)

        for key, label in PORTABLE_PROFILE_GROUPS:
            names = self._options[key]
            if not names:
                continue
            section = ctk.CTkFrame(list_frame, fg_color="transparent")
            section.pack(fill="x", padx=8, pady=(10, 2))
            ctk.CTkLabel(
                section,
                text=f"{label}（{len(names)}）",
                text_color=COLORS["text"],
                font=font(13, "bold"),
                anchor="w",
            ).pack(fill="x", pady=(0, 4))
            for name in names:
                variable = ctk.BooleanVar(value=True)
                self._variables[key][name] = variable
                ctk.CTkCheckBox(
                    section,
                    text=name,
                    variable=variable,
                    command=self._update_status,
                    text_color=COLORS["text"],
                    fg_color=COLORS["success"],
                    hover_color=COLORS["success_hover"],
                    border_color=COLORS["border"],
                    checkmark_color=COLORS["text"],
                    font=font(12),
                ).pack(fill="x", pady=3)

        self._update_status()

    def _selected_profiles(self) -> dict[str, list[str]]:
        return {
            key: [name for name in self._options[key] if self._variables[key][name].get()]
            for key, _label in PORTABLE_PROFILE_GROUPS
        }

    def _set_all(self, selected: bool) -> None:
        for variables in self._variables.values():
            for variable in variables.values():
                variable.set(selected)
        self._update_status()

    def _update_status(self) -> None:
        selected_count = sum(len(names) for names in self._selected_profiles().values())
        self._status.configure(text=f"已选择 {selected_count} / {self._total_count} 个 Profile")
        self._confirm_button.configure(state="normal" if selected_count else "disabled")
        if selected_count:
            self._error.configure(text="")

    def _confirm(self) -> None:
        selection = self._selected_profiles()
        if not any(selection.values()):
            self._error.configure(text="请至少选择一个 Profile")
            self._confirm_button.configure(state="disabled")
            return
        self.destroy()
        self._on_confirm(selection)
