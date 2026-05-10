import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font


class ProfileCard(ctk.CTkFrame):
    """A card widget displaying a profile summary with action buttons."""

    def __init__(self, master, name: str, info_lines: list[str], is_active: bool = False,
                 active_label: str = "当前运行", switch_label: str = "切换", on_switch=None, on_test=None,
                 on_edit=None, on_clone=None, on_delete=None, **kwargs):
        border_color = kwargs.pop("border_color", COLORS["success"] if is_active else COLORS["border_soft"])
        frame_kwargs = card_frame_kwargs(border_color)
        if is_active:
            frame_kwargs["fg_color"] = COLORS["surface_alt"]
        frame_kwargs.update(kwargs)
        super().__init__(master, **frame_kwargs)

        self._on_switch = on_switch
        self._on_test = on_test
        self._on_edit = on_edit
        self._on_clone = on_clone
        self._on_delete = on_delete
        self._name = name

        # Header row
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(12, 4))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        title_area.pack(side="left", fill="x", expand=True)

        indicator = ctk.CTkLabel(
            title_area,
            text="●" if is_active else "○",
            text_color=COLORS["success"] if is_active else COLORS["muted_soft"],
            font=font(15),
        )
        indicator.pack(side="left")

        name_label = ctk.CTkLabel(
            title_area,
            text=name,
            text_color=COLORS["text"],
            font=font(15, "bold"),
            anchor="w",
            justify="left",
        )
        name_label.pack(side="left", fill="x", expand=True, padx=(7, 0))
        bind_wraplength(title_area, name_label, padding=300, min_width=140, max_width=520)

        if is_active:
            active_tag = ctk.CTkLabel(
                title_area,
                text=active_label,
                fg_color=COLORS["success"],
                corner_radius=4,
                text_color=COLORS["app_bg"],
                font=font(11, "bold"),
                padx=7,
                pady=1,
            )
            active_tag.pack(side="left", padx=(8, 0))

        btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        btn_frame.pack(side="right")

        if not is_active and on_switch:
            ctk.CTkButton(
                btn_frame,
                text=switch_label,
                width=76 if len(switch_label) > 2 else 62,
                command=lambda: on_switch(name),
                **button_style("primary", compact=True),
            ).pack(side="left", padx=(0, 6))

        if on_test:
            ctk.CTkButton(
                btn_frame,
                text="测试",
                width=58,
                command=lambda: on_test(name),
                **button_style("accent", compact=True),
            ).pack(side="left", padx=(0, 6))

        if on_edit:
            ctk.CTkButton(
                btn_frame,
                text="编辑",
                width=58,
                command=lambda: on_edit(name),
                **button_style("secondary", compact=True),
            ).pack(side="left", padx=(0, 6))

        if on_clone:
            ctk.CTkButton(
                btn_frame,
                text="复制",
                width=58,
                command=lambda: on_clone(name),
                **button_style("secondary", compact=True),
            ).pack(side="left", padx=(0, 6))

        if on_delete:
            ctk.CTkButton(
                btn_frame,
                text="删除",
                width=58,
                command=lambda: on_delete(name),
                **button_style("danger", compact=True),
            ).pack(side="left")

        # Info lines
        info_frame = ctk.CTkFrame(self, fg_color="transparent")
        info_frame.pack(fill="x", padx=14, pady=(2, 12))
        for line in info_lines:
            lbl = ctk.CTkLabel(
                info_frame,
                text=line,
                text_color=COLORS["muted"],
                font=font(12),
                anchor="w",
                justify="left",
            )
            lbl.pack(fill="x")
            bind_wraplength(info_frame, lbl, padding=4)
