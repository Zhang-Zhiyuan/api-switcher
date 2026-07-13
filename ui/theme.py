import ctypes
import math
import re
import sys
import time
import tkinter
import customtkinter as ctk
from dataclasses import dataclass
from typing import Optional


DEFAULT_FONT_FAMILY = "Microsoft YaHei UI"
MONO_FONT_FAMILY = "Consolas"
_FONT_CACHE: dict[tuple[int, int, str, str], ctk.CTkFont] = {}
_SCROLL_START_EPSILON = 0.001
_SCROLL_END_EPSILON = 0.999
_SCROLL_CONSUMED_ATTR = "_api_switcher_scroll_consumed"
_SCROLL_ACTIVITY_ATTR = "_api_switcher_last_scroll_at"
_WINDOWS_SCROLL_DELTA_DIVISOR = 5
_WINDOWS_SCROLL_MIN_WHEEL_UNITS = 24
_WINDOWS_SCROLL_NOTCH_DELTA = 120
WINDOW_EDGE_MARGIN = 16

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
        magnitude = max(1, int(abs(delta) / _WINDOWS_SCROLL_DELTA_DIVISOR))
        if abs(delta) >= (_WINDOWS_SCROLL_NOTCH_DELTA / 2):
            magnitude = max(magnitude, _WINDOWS_SCROLL_MIN_WHEEL_UNITS)
        units = -magnitude if delta > 0 else magnitude
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

        try:
            if not self._parent_canvas.winfo_ismapped():
                return None
        except Exception:
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
    state = {"after_id": None, "wraplength": None}

    def update(_event=None):
        state["after_id"] = None
        try:
            if not label.winfo_exists():
                return
            width = container.winfo_width()
            try:
                scaling = float(container._get_widget_scaling())
            except (AttributeError, TypeError, ValueError):
                scaling = 1.0
            if scaling > 0:
                width = round(width / scaling)
            if width <= 1:
                wraplength = max(1, min(max_width, max(min_width, 1)))
            else:
                # Once a real width is available it must win over min_width;
                # otherwise the label itself can force a narrow container wider.
                wraplength = max(1, min(max_width, width - padding))
            if state.get("wraplength") == wraplength:
                return
            state["wraplength"] = wraplength
            label.configure(wraplength=wraplength)
        except Exception:
            return

    def schedule_update(_event=None):
        if state.get("after_id"):
            return
        try:
            state["after_id"] = container.after_idle(update)
        except Exception:
            update()

    try:
        container.bind("<Configure>", schedule_update, add="+")
    except TypeError:
        container.bind("<Configure>", schedule_update)
    schedule_update()


_GEOMETRY_SIZE_RE = re.compile(r"^(\d+)x(\d+)")


@dataclass(frozen=True)
class WindowLayout:
    """A window layout expressed in logical size and physical position units."""

    width: int
    height: int
    min_width: int
    min_height: int
    x: int
    y: int


def calculate_window_layout(
    preferred_size: tuple[int, int],
    minimum_size: tuple[int, int],
    screen_bounds: tuple[int, int, int, int],
    *,
    scaling: float = 1.0,
    master_bounds: tuple[int, int, int, int] | None = None,
    margin: int = WINDOW_EDGE_MARGIN,
) -> WindowLayout:
    """Fit a logical window size inside physical screen bounds.

    CustomTkinter scales geometry width/height but leaves x/y coordinates in
    physical pixels. Keeping those unit systems explicit prevents high-DPI
    windows from being scaled twice while they are centred.
    """

    try:
        scale = max(float(scaling), 0.1)
    except (TypeError, ValueError):
        scale = 1.0

    screen_x, screen_y, screen_width, screen_height = (int(value) for value in screen_bounds)
    screen_width = max(screen_width, 1)
    screen_height = max(screen_height, 1)
    physical_margin = max(0, round(max(0, int(margin)) * scale))
    max_horizontal_margin = max((screen_width - 1) // 2, 0)
    max_vertical_margin = max((screen_height - 1) // 2, 0)
    margin_x = min(physical_margin, max_horizontal_margin)
    margin_y = min(physical_margin, max_vertical_margin)

    safe_x = screen_x + margin_x
    safe_y = screen_y + margin_y
    safe_width = max(screen_width - margin_x * 2, 1)
    safe_height = max(screen_height - margin_y * 2, 1)
    available_width = max(1, math.floor(safe_width / scale))
    available_height = max(1, math.floor(safe_height / scale))

    preferred_width = max(1, int(preferred_size[0]))
    preferred_height = max(1, int(preferred_size[1]))
    requested_min_width = max(1, int(minimum_size[0]))
    requested_min_height = max(1, int(minimum_size[1]))
    width = min(max(preferred_width, requested_min_width), available_width)
    height = min(max(preferred_height, requested_min_height), available_height)
    min_width = min(requested_min_width, width)
    min_height = min(requested_min_height, height)

    physical_width = min(max(1, round(width * scale)), safe_width)
    physical_height = min(max(1, round(height * scale)), safe_height)

    if master_bounds is not None:
        master_x, master_y, master_width, master_height = (int(value) for value in master_bounds)
        x = master_x + (max(master_width, 1) - physical_width) // 2
        y = master_y + (max(master_height, 1) - physical_height) // 2
    else:
        x = safe_x + (safe_width - physical_width) // 2
        y = safe_y + (safe_height - physical_height) // 2

    max_x = safe_x + max(safe_width - physical_width, 0)
    max_y = safe_y + max(safe_height - physical_height, 0)
    x = min(max(x, safe_x), max_x)
    y = min(max(y, safe_y), max_y)
    return WindowLayout(width, height, min_width, min_height, x, y)


def _configured_window_size(window, configured_geometry: str) -> tuple[int, int]:
    try:
        # CTk keeps the requested logical size here even while a new toplevel
        # is still withdrawn and Tk reports its temporary 200x200 geometry.
        width = int(window._current_width)
        height = int(window._current_height)
    except (AttributeError, TypeError, ValueError):
        width = height = 0

    match = _GEOMETRY_SIZE_RE.match(configured_geometry)
    if (width <= 1 or height <= 1) and match:
        width = int(match.group(1))
        height = int(match.group(2))
    elif width <= 1 or height <= 1:
        scaling = _window_scaling(window)
        width = round(window.winfo_width() / scaling)
        height = round(window.winfo_height() / scaling)

    scaling = _window_scaling(window)
    if width <= 1:
        width = round(window.winfo_reqwidth() / scaling)
    if height <= 1:
        height = round(window.winfo_reqheight() / scaling)

    return max(width, 1), max(height, 1)


def _window_scaling(window) -> float:
    try:
        return max(float(window._get_window_scaling()), 0.1)
    except (AttributeError, TypeError, ValueError):
        return 1.0


def _window_minimum_size(window, fallback: tuple[int, int]) -> tuple[int, int]:
    try:
        width = int(window._min_width)
        height = int(window._min_height)
        return max(width, 1), max(height, 1)
    except (AttributeError, TypeError, ValueError):
        pass

    try:
        scaling = _window_scaling(window)
        width, height = tkinter.Wm.minsize(window)
        return max(1, round(width / scaling)), max(1, round(height / scaling))
    except Exception:
        return max(1, int(fallback[0])), max(1, int(fallback[1]))


def _windows_work_area(window) -> tuple[int, int, int, int] | None:
    if not sys.platform.startswith("win"):
        return None

    try:
        from ctypes import wintypes

        class MonitorInfo(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        hwnd = user32.GetAncestor(window.winfo_id(), 2) or window.winfo_id()
        monitor = user32.MonitorFromWindow(hwnd, 2)
        info = MonitorInfo(cbSize=ctypes.sizeof(MonitorInfo))
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            rect = info.rcWork
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width > 0 and height > 0:
                return rect.left, rect.top, width, height
    except Exception:
        return None
    return None


def _screen_bounds(window) -> tuple[int, int, int, int]:
    work_area = _windows_work_area(window)
    if work_area is not None:
        return work_area
    try:
        bounds = (
            window.winfo_vrootx(),
            window.winfo_vrooty(),
            window.winfo_vrootwidth(),
            window.winfo_vrootheight(),
        )
        if bounds[2] > 1 and bounds[3] > 1:
            return bounds
    except Exception:
        pass
    return (0, 0, window.winfo_screenwidth(), window.winfo_screenheight())


def fit_window_to_screen(
    window,
    master=None,
    *,
    preferred_size: tuple[int, int] | None = None,
    minimum_size: tuple[int, int] | None = None,
    margin: int = WINDOW_EDGE_MARGIN,
    activate: bool = False,
    make_transient: bool = False,
) -> WindowLayout:
    """Resize and position a Tk/CustomTkinter window inside its monitor."""

    configured_geometry = window.geometry()
    configured_size = _configured_window_size(window, configured_geometry)
    preferred = preferred_size or configured_size
    minimum = minimum_size or _window_minimum_size(window, preferred)
    scaling = _window_scaling(window)

    master_bounds = None
    try:
        if master is not None and master.winfo_exists() and master.winfo_width() > 1:
            master_bounds = (
                master.winfo_rootx(),
                master.winfo_rooty(),
                master.winfo_width(),
                master.winfo_height(),
            )
    except Exception:
        master_bounds = None

    layout = calculate_window_layout(
        preferred,
        minimum,
        _screen_bounds(master or window),
        scaling=scaling,
        master_bounds=master_bounds,
        margin=margin,
    )
    window.minsize(layout.min_width, layout.min_height)
    window.geometry(f"{layout.width}x{layout.height}+{layout.x}+{layout.y}")

    if make_transient and master is not None:
        try:
            if master.winfo_exists():
                window.transient(master)
        except Exception:
            pass

    if activate:
        window.lift()
        window.focus_force()
    return layout


def center_window(window, master=None) -> None:
    window.update_idletasks()
    fit_window_to_screen(
        window,
        master,
        activate=True,
        make_transient=True,
    )
