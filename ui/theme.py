import customtkinter as ctk
from typing import Optional


DEFAULT_FONT_FAMILY = "Microsoft YaHei UI"
MONO_FONT_FAMILY = "Consolas"

COLORS = {
    "app_bg": "#0f1419",
    "surface": "#171d24",
    "surface_alt": "#202833",
    "surface_hover": "#27313d",
    "border": "#334151",
    "border_soft": "#263241",
    "text": "#f4f7fb",
    "muted": "#9aa8b7",
    "muted_soft": "#738091",
    "primary": "#3b82f6",
    "primary_hover": "#2563eb",
    "success": "#22c55e",
    "success_hover": "#16a34a",
    "danger": "#ef4444",
    "danger_hover": "#dc2626",
    "warning": "#f59e0b",
    "warning_hover": "#d97706",
    "secondary": "#344255",
    "secondary_hover": "#43546a",
    "accent": "#06b6d4",
    "accent_hover": "#0891b2",
}


def font(size: int, weight: Optional[str] = None, family: Optional[str] = None) -> ctk.CTkFont:
    kwargs = {"size": size, "family": family or DEFAULT_FONT_FAMILY}
    if weight:
        kwargs["weight"] = weight
    return ctk.CTkFont(**kwargs)


def card_frame_kwargs(border_color: Optional[str] = None) -> dict:
    return {
        "corner_radius": 8,
        "fg_color": COLORS["surface"],
        "border_width": 1,
        "border_color": border_color or COLORS["border_soft"],
    }


def button_style(kind: str = "primary", compact: bool = False) -> dict:
    palette = {
        "primary": ("primary", "primary_hover", "text"),
        "secondary": ("secondary", "secondary_hover", "text"),
        "danger": ("danger", "danger_hover", "text"),
        "warning": ("warning", "warning_hover", "text"),
        "success": ("success", "success_hover", "text"),
        "accent": ("accent", "accent_hover", "text"),
    }
    fg, hover, text = palette.get(kind, palette["primary"])
    return {
        "height": 28 if compact else 34,
        "corner_radius": 6,
        "fg_color": COLORS[fg],
        "hover_color": COLORS[hover],
        "text_color": COLORS[text],
        "font": font(12, "bold"),
    }


def input_style() -> dict:
    return {
        "height": 34,
        "corner_radius": 6,
        "fg_color": COLORS["app_bg"],
        "border_color": COLORS["border"],
        "text_color": COLORS["text"],
    }


def combo_style() -> dict:
    style = input_style()
    style.update({
        "button_color": COLORS["secondary"],
        "button_hover_color": COLORS["secondary_hover"],
        "dropdown_fg_color": COLORS["surface"],
        "dropdown_hover_color": COLORS["surface_hover"],
        "dropdown_text_color": COLORS["text"],
    })
    return style


def textbox_style(monospace: bool = False) -> dict:
    return {
        "fg_color": COLORS["app_bg"],
        "border_color": COLORS["border"],
        "border_width": 1,
        "text_color": COLORS["text"],
        "scrollbar_button_color": COLORS["secondary"],
        "scrollbar_button_hover_color": COLORS["secondary_hover"],
        "font": font(12, family=MONO_FONT_FAMILY if monospace else DEFAULT_FONT_FAMILY),
        "corner_radius": 8,
    }


def bind_wraplength(container, label, padding: int = 32, min_width: int = 220, max_width: int = 980) -> None:
    """Keep CTkLabel wraplength responsive to its container."""
    def update(_event=None):
        width = container.winfo_width()
        if width <= 1:
            width = max_width
        label.configure(wraplength=max(min_width, min(max_width, width - padding)))

    try:
        container.bind("<Configure>", update, add="+")
    except TypeError:
        container.bind("<Configure>", update)
    update()


def center_window(window, master=None) -> None:
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()

    if master is not None and master.winfo_width() > 1:
        x = master.winfo_rootx() + (master.winfo_width() - width) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height) // 2
    else:
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2

    window.geometry(f"+{max(x, 0)}+{max(y, 0)}")
