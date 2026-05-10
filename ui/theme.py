import re
import customtkinter as ctk
from typing import Optional


DEFAULT_FONT_FAMILY = "Microsoft YaHei UI"
MONO_FONT_FAMILY = "Consolas"

COLORS = {
    "app_bg": "#101216",
    "surface": "#181b21",
    "surface_alt": "#20252d",
    "surface_hover": "#29313b",
    "field_bg": "#11151b",
    "border": "#39424f",
    "border_soft": "#2a323d",
    "text": "#f3f6fb",
    "muted": "#a0a9b5",
    "muted_soft": "#737d8a",
    "primary": "#3578f6",
    "primary_hover": "#2563eb",
    "success": "#2fbf71",
    "success_hover": "#24995a",
    "danger": "#ef4444",
    "danger_hover": "#dc2626",
    "warning": "#e6a23c",
    "warning_hover": "#c98218",
    "secondary": "#303946",
    "secondary_hover": "#414c5d",
    "accent": "#14a6a8",
    "accent_hover": "#0d8688",
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
        "fg_color": COLORS["field_bg"],
        "border_color": COLORS["border"],
        "text_color": COLORS["text"],
        "placeholder_text_color": COLORS["muted_soft"],
        "font": font(12),
    }


def combo_style() -> dict:
    style = input_style()
    style.pop("placeholder_text_color", None)
    style.update({
        "button_color": COLORS["secondary"],
        "button_hover_color": COLORS["secondary_hover"],
        "dropdown_fg_color": COLORS["surface"],
        "dropdown_hover_color": COLORS["surface_hover"],
        "dropdown_text_color": COLORS["text"],
        "dropdown_font": font(12),
    })
    return style


def textbox_style(monospace: bool = False) -> dict:
    return {
        "fg_color": COLORS["field_bg"],
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


_GEOMETRY_SIZE_RE = re.compile(r"^(\d+)x(\d+)")


def _configured_window_size(window, configured_geometry: str) -> tuple[int, int]:
    width = window.winfo_width()
    height = window.winfo_height()

    if width <= 1 or height <= 1:
        match = _GEOMETRY_SIZE_RE.match(configured_geometry)
        if match:
            width = int(match.group(1))
            height = int(match.group(2))

    if width <= 1:
        width = window.winfo_reqwidth()
    if height <= 1:
        height = window.winfo_reqheight()

    return max(width, 1), max(height, 1)


def _screen_bounds(window) -> tuple[int, int, int, int]:
    try:
        return (
            window.winfo_vrootx(),
            window.winfo_vrooty(),
            window.winfo_vrootwidth(),
            window.winfo_vrootheight(),
        )
    except Exception:
        return (0, 0, window.winfo_screenwidth(), window.winfo_screenheight())


def center_window(window, master=None) -> None:
    configured_geometry = window.geometry()
    window.update_idletasks()
    width, height = _configured_window_size(window, configured_geometry)

    if master is not None and master.winfo_exists() and master.winfo_width() > 1:
        x = master.winfo_rootx() + (master.winfo_width() - width) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height) // 2
    else:
        screen_x, screen_y, screen_width, screen_height = _screen_bounds(window)
        x = screen_x + (screen_width - width) // 2
        y = screen_y + (screen_height - height) // 2

    screen_x, screen_y, screen_width, screen_height = _screen_bounds(window)
    max_x = screen_x + max(screen_width - width, 0)
    max_y = screen_y + max(screen_height - height, 0)
    x = min(max(x, screen_x), max_x)
    y = min(max(y, screen_y), max_y)

    if master is not None and master.winfo_exists():
        try:
            window.transient(master)
        except Exception:
            pass

    window.geometry(f"{width}x{height}+{x}+{y}")
    window.lift()
    window.focus_force()
