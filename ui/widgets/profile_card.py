import customtkinter as ctk

from ui.theme import COLORS, bind_wraplength, button_style, card_frame_kwargs, configure_if_changed, font


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
            use_ancestor_width = width <= 1
            if use_ancestor_width:
                # A just-created frame has no geometry yet. Starting every
                # card with a one-column toolbar causes a tall layout followed
                # by an expensive full-list reflow when its real width arrives.
                parent = getattr(container, "master", None)
                while parent is not None and width <= 1:
                    width = parent.winfo_width()
                    parent = getattr(parent, "master", None)
            try:
                scaling = float(container._get_widget_scaling())
            except (AttributeError, TypeError, ValueError):
                scaling = 1.0
            if scaling > 0:
                width = round(width / scaling)
            if use_ancestor_width:
                # The card's 14px side paddings are CTk logical units too.
                width = max(1, width - 28)
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
                 on_edit=None, on_clone=None, on_delete=None, on_export=None, **kwargs):
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
        self._on_export = on_export
        self._name = name
        self._is_active = is_active
        self._inactive_color = kwargs.get("fg_color", COLORS["surface"])
        self._action_buttons = {}
        self._actions_signature = None
        self._btn_frame = None
        self._info_labels = []

        # Header row
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(12, 4))

        title_area = ctk.CTkFrame(header, fg_color="transparent")
        self._title_area = title_area
        title_area.pack(side="left", fill="x", expand=True)

        indicator = ctk.CTkLabel(
            title_area,
            text="●" if is_active else "○",
            text_color=COLORS["success"] if is_active else COLORS["muted_soft"],
            font=font(15),
        )
        indicator.pack(side="left")
        self._indicator = indicator

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
        self._name_label = name_label
        self._active_tag = None

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
            self._active_tag = active_tag

        self._actions_row = ctk.CTkFrame(self, fg_color="transparent")
        self._sync_actions(switch_label)

        # Info lines
        self._info_frame = ctk.CTkFrame(self, fg_color="transparent")
        self._info_frame.pack(fill="x", padx=14, pady=(2 if not self._action_buttons else 0, 12))
        self._sync_info(info_lines)

    @property
    def test_button(self):
        return self._action_buttons.get("test")

    def _invoke_action(self, action):
        # Resolve the current callback at invocation, never one captured before
        # a refresh revoked an account's switch/export permission.
        callback = getattr(self, f"_on_{action}", None)
        if callback is not None and (action != "switch" or not self._is_active):
            return callback(self._name)

    def _sync_actions(self, switch_label):
        actions = []
        if not self._is_active and self._on_switch:
            actions.append(("switch", switch_label, 76 if len(switch_label) > 2 else 62, "primary"))

        if self._on_test:
            actions.append(("test", "测试", 58, "secondary"))

        if self._on_edit:
            actions.append(("edit", "编辑", 58, "secondary"))

        if self._on_clone:
            actions.append(("clone", "复制", 58, "secondary"))

        if self._on_export:
            actions.append(("export", "导出登录", 86, "secondary"))

        if self._on_delete:
            actions.append(("delete", "删除", 58, "danger"))

        signature = tuple(actions)
        if signature == self._actions_signature:
            return  # Preserve test progress/disabled state on text-only edits.
        # A failed partial rebuild must be retried, even if the next refresh
        # returns to the previous action set.
        self._actions_signature = None
        if self._btn_frame is not None:
            self._btn_frame.destroy()
            self._btn_frame = None
        self._action_buttons = {}
        if actions:
            pack = {"fill": "x", "padx": 14, "pady": (0, 8)}
            if hasattr(self, "_info_frame"):
                pack["before"] = self._info_frame
            self._actions_row.pack(**pack)
            btn_frame = self._btn_frame = ctk.CTkFrame(self._actions_row, fg_color="transparent")
            btn_frame.pack(fill="x")
            action_buttons = []
            for action, text, width, kind in actions:
                button = ctk.CTkButton(
                    btn_frame,
                    text=text,
                    width=width,
                    command=lambda key=action: self._invoke_action(key),
                    **({**button_style("secondary", compact=True), "text_color": COLORS["danger"]}
                       if kind == "danger" else button_style(kind, compact=True)),
                )
                action_buttons.append(button)
                self._action_buttons[action] = button
            _bind_profile_card_action_grid(btn_frame, action_buttons)
        else:
            self._actions_row.pack_forget()
        if hasattr(self, "_info_frame"):
            self._info_frame.pack_configure(pady=(0 if actions else 2, 12))
        self._actions_signature = signature

    def _sync_info(self, info_lines):
        # Reuse spare labels: destroying only a label would leave its wrapping
        # callback on the still-live container, accumulating after every edit.
        for label in self._info_labels[len(info_lines):]:
            configure_if_changed(label, text="")
            if label.winfo_manager():
                label.pack_forget()
        for index, line in enumerate(info_lines):
            if index < len(self._info_labels):
                label = self._info_labels[index]
                configure_if_changed(label, text=line)
                if not label.winfo_manager():
                    label.pack(fill="x")
                continue
            lbl = ctk.CTkLabel(
                self._info_frame,
                text=line,
                text_color=COLORS["muted"],
                font=font(12),
                anchor="w",
                justify="left",
            )
            lbl.pack(fill="x")
            bind_wraplength(self._info_frame, lbl, padding=4)
            self._info_labels.append(lbl)

    def update_content(self, name, info_lines, is_active=False, active_label="当前运行", switch_label="切换",
                       on_switch=None, on_test=None, on_edit=None, on_clone=None, on_delete=None, on_export=None,
                       border_color=None):
        """Refresh a card in place; a summary edit must not unmap the list."""
        self._name = name
        self._is_active = is_active
        for action, callback in (("switch", on_switch), ("test", on_test), ("edit", on_edit),
                                 ("clone", on_clone), ("delete", on_delete), ("export", on_export)):
            setattr(self, f"_on_{action}", callback)
        configure_if_changed(self, border_color=border_color or (COLORS["success"] if is_active else COLORS["border_soft"]),
                             fg_color=COLORS["surface_alt"] if is_active else self._inactive_color)
        configure_if_changed(self._name_label, text=name)
        configure_if_changed(self._indicator, text="●" if is_active else "○",
                             text_color=COLORS["success"] if is_active else COLORS["muted_soft"])
        if is_active:
            if self._active_tag is None:
                self._active_tag = ctk.CTkLabel(
                    self._title_area, text=active_label, fg_color=COLORS["success"], corner_radius=4,
                    text_color=COLORS["app_bg"], font=font(11, "bold"), padx=7, pady=1,
                )
            else:
                configure_if_changed(self._active_tag, text=active_label)
            if not self._active_tag.winfo_manager():
                self._active_tag.pack(side="left", padx=(8, 0))
        elif self._active_tag is not None:
            self._active_tag.pack_forget()
        self._sync_actions(switch_label)
        self._sync_info(info_lines)
