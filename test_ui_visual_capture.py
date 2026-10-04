"""Capture ordering and refusal checks without a GUI or desktop screenshots."""
from types import SimpleNamespace

from PIL import ImageGrab
import pytest

from tools import ui_visual_audit


class Function:
    def __init__(self, action):
        self.action = action

    def __call__(self, *args):
        return self.action(*args)


class Window:
    def __init__(self, events):
        self.events = events
        self.mapped = True
        self.handle = 100
        self.topmost = False

    def update_idletasks(self):
        self.events.append("idle")

    def winfo_ismapped(self):
        return self.mapped

    def winfo_id(self):
        return self.handle

    def attributes(self, name, *values):
        assert name == "-topmost"
        if values:
            self.topmost = values[0]
        return self.topmost

    def lift(self):
        pass

    def focus_force(self):
        pass

    def update(self):
        pass


@pytest.fixture
def capture(monkeypatch):
    events, grabs = [], []
    state = {"redraw": 1, "flush": 0, "foreground": 200, "roots": {100: 200, 200: 200},
             "rect_ok": True, "rect_width": 400, "change_focus_on_grab": False}

    def root(hwnd, flag):
        assert flag == 2  # No owner-based equivalence for another dialog.
        return state["roots"].get(hwnd, hwnd)

    def redraw(hwnd, rect, region, flags):
        assert (hwnd, rect, region, flags) == (200, None, None, 0x185)
        events.append("redraw")
        return state["redraw"]

    def flush():
        events.append("flush")
        return state["flush"]

    def grab(**kwargs):
        events.append("grab")
        grabs.append(kwargs)
        assert kwargs in ({"window": 200}, {"bbox": (10, 20, 410, 320), "all_screens": True}), "unbounded desktop capture is forbidden"
        if state["change_focus_on_grab"]:
            state["foreground"] = 900
        return "synthetic-image"

    def client_rect(hwnd, target):
        assert hwnd == 200
        target._obj.right, target._obj.bottom = state["rect_width"], 300
        return state["rect_ok"]

    def client_origin(hwnd, target):
        assert hwnd == 200
        target._obj.x, target._obj.y = 10, 20
        return 1

    dlls = SimpleNamespace(
        user32=SimpleNamespace(GetAncestor=Function(root), RedrawWindow=Function(redraw),
                               GetForegroundWindow=Function(lambda: state["foreground"]),
                               SetForegroundWindow=Function(lambda hwnd: state.setdefault("activation_requests", []).append(hwnd)),
                               GetClientRect=Function(client_rect), ClientToScreen=Function(client_origin)),
        dwmapi=SimpleNamespace(DwmFlush=Function(flush)),
    )
    monkeypatch.setattr(ui_visual_audit, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ui_visual_audit.ctypes, "windll", dlls, raising=False)
    monkeypatch.setattr(ImageGrab, "grab", grab)
    return Window(events), events, grabs, state


def test_native_capture_finishes_tk_and_dwm_paint_before_grabbing(capture):
    window, events, grabs, _state = capture
    assert ui_visual_audit.capture_window_image(window) == "synthetic-image"
    assert events == ["idle", "redraw", "idle", "flush", "grab"]
    assert grabs == [{"window": 200}]


@pytest.mark.parametrize("phase", ["redraw", "flush"])
def test_failed_repaint_or_compositor_never_falls_back_to_desktop(capture, phase):
    window, events, grabs, state = capture
    state[phase] = 0 if phase == "redraw" else -1
    with pytest.raises(RuntimeError):
        ui_visual_audit.capture_window_image(window)
    assert not grabs and "grab" not in events


@pytest.mark.parametrize("invalid", ["unmapped", "missing_handle"])
def test_invalid_preview_never_repaints_or_captures(capture, invalid):
    window, events, grabs, state = capture
    if invalid == "unmapped":
        window.mapped = False
    else:
        state["roots"][100] = 0
    with pytest.raises(RuntimeError):
        ui_visual_audit.capture_window_image(window)
    assert "redraw" not in events and not grabs


def test_changed_native_handle_during_paint_is_refused(capture):
    window, _events, grabs, _state = capture
    count = 0

    def idle():
        nonlocal count
        count += 1
        if count == 2:
            window.handle = 300

    window.update_idletasks = idle
    with pytest.raises(RuntimeError, match="changed"):
        ui_visual_audit.capture_window_image(window)
    assert not grabs


def test_explicit_foreground_mode_rechecks_wrapper_and_limits_capture_to_client_rect(capture):
    window, _events, grabs, state = capture
    state["foreground"] = 201
    state["roots"][201] = 200
    window.handle = 99
    window.update = lambda: setattr(window, "handle", 100)
    assert ui_visual_audit.capture_window_image(window, onscreen=True) == "synthetic-image"
    assert grabs == [{"bbox": (10, 20, 410, 320), "all_screens": True}]
    assert window.topmost is False
    assert state["activation_requests"] == [200]


def test_foreign_foreground_refuses_capture_and_restores_topmost(capture):
    window, events, grabs, state = capture
    state["foreground"] = 900
    with pytest.raises(RuntimeError, match="foreground"):
        ui_visual_audit.capture_window_image(window, onscreen=True)
    assert window.topmost is False
    assert "redraw" not in events and not grabs


@pytest.mark.parametrize("fault", ["missing", "empty"])
def test_foreground_comparison_refuses_unavailable_client_bounds(capture, fault):
    window, _events, grabs, state = capture
    state["rect_ok"] = fault != "missing"
    state["rect_width"] = 0 if fault == "empty" else 400
    with pytest.raises(RuntimeError, match="client rectangle"):
        ui_visual_audit.capture_window_image(window, onscreen=True)
    assert not grabs and window.topmost is False


def test_foreground_change_during_comparison_discards_result(capture):
    window, _events, grabs, state = capture
    state["change_focus_on_grab"] = True
    with pytest.raises(RuntimeError, match="foreground"):
        ui_visual_audit.capture_window_image(window, onscreen=True)
    assert grabs == [{"bbox": (10, 20, 410, 320), "all_screens": True}]
    assert window.topmost is False
