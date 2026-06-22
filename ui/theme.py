import re
import sys
import time
import tkinter
import customtkinter as ctk
from typing import Optional


DEFAULT_FONT_FAMILY = "Microsoft YaHei UI"
MONO_FONT_FAMILY = "Consolas"
_FONT_CACHE: dict[tuple[int, int, str, str], ctk.CTkFont] = {}
_SCROLL_START_EPSILON = 0.001
_SCROLL_END_EPSILON = 0.999
_SCROLL_CONSUMED_ATTR = "_api_switcher_scroll_consumed"
_SCROLL_ACTIVITY_ATTR = "_api_switcher_last_scroll_at"
_WINDOWS_SCROLL_DELTA_DIVISOR = 5

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


def _event_scroll_chain(event):
    cached = getattr(event, "_api_switcher_scroll_chain", None)
    if cached is not None:
        return cached

    chain = []
    seen = set()
    current = getattr(event, "widget", None)
    while current is not None:
        candidate = None
        if current.__class__.__name__ == "CTkTextbox":
            candidate = getattr(current, "_textbox", current)
        else:
            child_canvas = getattr(current, "_parent_canvas", None)
            if child_canvas is not None:
                candidate = child_canvas
            elif isinstance(current, (tkinter.Canvas, tkinter.Text)):
                candidate = current
        if candidate is not None:
            marker = id(candidate)
            if marker not in seen:
                seen.add(marker)
                chain.append(candidate)
        current = getattr(current, "master", None)

    cached = tuple(chain)
    try:
        setattr(event, "_api_switcher_scroll_chain", cached)
    except Exception:
        pass
    return cached


def _event_scroll_consumed(event) -> bool:
    return bool(getattr(event, _SCROLL_CONSUMED_ATTR, False))


def _mark_event_scroll_consumed(event) -> None:
    try:
        setattr(event, _SCROLL_CONSUMED_ATTR, True)
    except Exception:
        pass


def _mark_scroll_activity(widget) -> None:
    if widget is None:
        return
    try:
        target = widget.winfo_toplevel()
    except Exception:
        target = widget
    try:
        setattr(target, _SCROLL_ACTIVITY_ATTR, time.perf_counter())
    except Exception:
        pass


def recent_user_scroll(widget, idle_ms: int = 140) -> bool:
    try:
        target = widget.winfo_toplevel()
    except Exception:
        target = widget
    try:
        last_scroll = float(getattr(target, _SCROLL_ACTIVITY_ATTR, 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if last_scroll <= 0.0:
        return False
    return (time.perf_counter() - last_scroll) * 1000 < max(1, int(idle_ms))


def _wheel_delta(event) -> float:
    try:
        delta = float(getattr(event, "delta", 0) or 0)
    except (TypeError, ValueError):
        delta = 0.0
    if delta:
        return delta

    try:
        button_num = int(getattr(event, "num", 0) or 0)
    except (TypeError, ValueError):
        button_num = 0
    if button_num == 4:
        return 1.0
    if button_num == 5:
        return -1.0
    return 0.0


def _wheel_direction(event) -> int:
    delta = _wheel_delta(event)
    if delta > 0:
        return 1
    if delta < 0:
        return -1
    return 0


def _scroll_view(widget, horizontal: bool = False) -> tuple[float, float]:
    method_name = "xview" if horizontal else "yview"
    method = getattr(widget, method_name, None)
    if not callable(method):
        return (0.0, 1.0)
    try:
        first, last = method()
        return float(first), float(last)
    except Exception:
        return (0.0, 1.0)


def _scroll_widget_can_consume(widget, event, horizontal: bool = False) -> bool:
    first, last = _scroll_view(widget, horizontal=horizontal)
    if first <= 0.0 and last >= 1.0:
        return False
    direction = _wheel_direction(event)
    if direction > 0:
        return first > _SCROLL_START_EPSILON
    if direction < 0:
        return last < _SCROLL_END_EPSILON
    return False


def _scroll_units(event) -> int:
    delta = _wheel_delta(event)
    if not delta:
        return 0

    if sys.platform.startswith("win"):
        units = -int(delta / _WINDOWS_SCROLL_DELTA_DIVISOR)
    else:
        units = -int(delta)
    if units == 0:
        return -1 if delta > 0 else 1
    return units


def _scroll_widget(widget, event, horizontal: bool = False) -> bool:
    if not _scroll_widget_can_consume(widget, event, horizontal=horizontal):
        return False

    units = _scroll_units(event)
    if not units:
        return False

    method_name = "xview" if horizontal else "yview"
    method = getattr(widget, method_name, None)
    if not callable(method):
        return False
    try:
        method("scroll", units, "units")
        return True
    except Exception:
        return False


def _scroll_chain_index(chain, target) -> int:
    for index, candidate in enumerate(chain):
        if candidate is target:
            return index
    raise ValueError


def _patch_nested_scrollable_frame_mousewheel() -> None:
    scrollable_cls = ctk.CTkScrollableFrame
    if getattr(scrollable_cls, "_api_switcher_nested_scroll_guard", False):
        return

    def guarded_mouse_wheel_all(self, event):
        if _event_scroll_consumed(event):
            return None

        _mark_scroll_activity(getattr(event, "widget", None))
        chain = _event_scroll_chain(event)
        try:
            parent_index = _scroll_chain_index(chain, self._parent_canvas)
        except ValueError:
            return None
        horizontal = bool(getattr(self, "_shift_pressed", False))
        for child_scroll in chain[:parent_index]:
            if _scroll_widget_can_consume(child_scroll, event, horizontal=horizontal):
                return None
        if _scroll_widget(self._parent_canvas, event, horizontal=horizontal):
            _mark_event_scroll_consumed(event)
        return None

    scrollable_cls._mouse_wheel_all = guarded_mouse_wheel_all
    scrollable_cls._api_switcher_nested_scroll_guard = True


_patch_nested_scrollable_frame_mousewheel()


def font(size: int, weight: Optional[str] = None, family: Optional[str] = None) -> ctk.CTkFont:
    resolved_family = family or DEFAULT_FONT_FAMILY
    resolved_weight = weight or ""
    root = tkinter._default_root
    key = (id(root), int(size), resolved_weight, resolved_family)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    kwargs = {"size": size, "family": resolved_family}
    if weight:
        kwargs["weight"] = weight
    created = ctk.CTkFont(**kwargs)
    _FONT_CACHE[key] = created
    return created


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
