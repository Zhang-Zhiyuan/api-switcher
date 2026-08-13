import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, font


def _profile_card_action_columns(width: int, item_count: int) -> int:
    """Choose readable action columns for the card's logical width."""

    count = max(1, int(item_count))
    available = max(1, int(width))
    if available >= 420:
        columns = 5
    elif available >= 280:
        columns = 3
    elif available >= 180:
        columns = 2
    else:
        columns = 1
    return min(count, columns)


def _bind_profile_card_action_grid(container, buttons) -> None:
    """Keep profile actions reachable without overflowing at high DPI."""

    widgets = tuple(buttons)
    if not widgets:
        return
    state = {"columns": 0}

    def apply_layout(event=None):
        try:
            width = int(getattr(event, "width", 0) or container.winfo_width())
            try:
                scaling = float(container._get_widget_scaling())
            except (AttributeError, TypeError, ValueError):
                scaling = 1.0
            if scaling > 0:
                width = round(width / scaling)
            columns = _profile_card_action_columns(width, len(widgets))
            if columns == state["columns"]:
                return

            previous = state["columns"]
            state["columns"] = columns
            for column in range(max(previous, columns)):
                container.grid_columnconfigure(
                    column,
                    weight=1 if column < columns else 0,
                    minsize=0,
                    uniform="profile-card-actions" if column < columns else "",
                )
            for index, button in enumerate(widgets):
                column = index % columns
                has_following_row = index // columns < (len(widgets) - 1) // columns
                button.grid(
                    row=index // columns,
                    column=column,
                    sticky="ew",
                    padx=(0 if column == 0 else 6, 0),
                    pady=(0, 6 if has_following_row else 0),
                )
        except Exception:
            return

    container.bind("<Configure>", apply_layout, add="+")
    apply_layout()


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
        bind_wraplength(title_area, name_label, padding=120, min_width=160, max_width=760)

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

        actions = []

        if not is_active and on_switch:
            actions.append((switch_label, 76 if len(switch_label) > 2 else 62, "primary", lambda: on_switch(name)))

        if on_test:
            actions.append(("测试", 58, "accent", lambda: on_test(name)))

        if on_edit:
            actions.append(("编辑", 58, "secondary", lambda: on_edit(name)))

        if on_clone:
            actions.append(("复制", 58, "secondary", lambda: on_clone(name)))

        if on_delete:
            actions.append(("删除", 58, "danger", lambda: on_delete(name)))

        if actions:
            actions_row = ctk.CTkFrame(self, fg_color="transparent")
            actions_row.pack(fill="x", padx=14, pady=(0, 8))
            btn_frame = ctk.CTkFrame(actions_row, fg_color="transparent")
            btn_frame.pack(fill="x")
            action_buttons = []
            for text, width, kind, command in actions:
                button = ctk.CTkButton(
                    btn_frame,
                    text=text,
                    width=width,
                    command=command,
                    **button_style(kind, compact=True),
                )
                action_buttons.append(button)
            _bind_profile_card_action_grid(btn_frame, action_buttons)

        # Info lines
        info_frame = ctk.CTkFrame(self, fg_color="transparent")
        info_frame.pack(fill="x", padx=14, pady=(2 if not actions else 0, 12))
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
