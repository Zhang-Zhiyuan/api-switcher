from __future__ import annotations

from ui import theme
from ui.app import MAIN_LAYOUT_COMPACT_MIN_WIDTH, MAIN_LAYOUT_WIDE_MIN_WIDTH, main_layout_mode
from ui.widgets.adaptive_tab_bar import adaptive_tab_columns
from ui.widgets.toast import _toast_wraplength
from ui.startup_splash import _splash_layout
from ui.dialogs.auto_continue_logs_dialog import _auto_continue_logs_layout
from ui.dialogs.git_snapshot_history_dialog import _git_history_layout


def test_window_layout_preserves_preferred_size_on_large_screen():
    layout = theme.calculate_window_layout(
        (1120, 760),
        (480, 460),
        (0, 0, 1920, 1080),
    )

    assert layout == theme.WindowLayout(1120, 760, 480, 460, 400, 160)


def test_window_layout_fits_small_high_dpi_work_area_without_double_scaling():
    layout = theme.calculate_window_layout(
        (1120, 760),
        (480, 460),
        (0, 0, 1366, 768),
        scaling=1.25,
    )

    assert layout.width == 1060
    assert layout.height == 582
    assert round(layout.width * 1.25) <= 1366 - 40
    assert round(layout.height * 1.25) <= 768 - 40
    assert layout.min_width <= layout.width
    assert layout.min_height <= layout.height


def test_window_layout_caps_minimum_and_handles_tiny_screen():
    layout = theme.calculate_window_layout(
        (1120, 760),
        (980, 620),
        (0, 0, 800, 600),
    )

    assert layout == theme.WindowLayout(768, 568, 768, 568, 16, 16)


def test_window_layout_centres_on_negative_coordinate_monitor():
    layout = theme.calculate_window_layout(
        (1120, 760),
        (480, 460),
        (-1920, 0, 1920, 1080),
    )

    assert layout.x == -1520
    assert layout.y == 160


def test_window_layout_clamps_master_centre_to_visible_area():
    layout = theme.calculate_window_layout(
        (620, 520),
        (520, 420),
        (0, 0, 800, 600),
        master_bounds=(700, 500, 200, 100),
    )

    assert (layout.x, layout.y) == (164, 64)


class _FakeWindow:
    def __init__(self):
        self._current_width = 620
        self._current_height = 520
        self._min_width = 520
        self._min_height = 420
        self.geometry_calls = []
        self.minimum_calls = []

    def geometry(self, value=None):
        if value is None:
            # A newly-created CTkToplevel reports this temporary physical size.
            return "133x133+10+10"
        self.geometry_calls.append(value)

    def minsize(self, width, height):
        self.minimum_calls.append((width, height))

    def _get_window_scaling(self):
        return 1.5

    def winfo_width(self):
        return 200

    def winfo_height(self):
        return 200

    def winfo_reqwidth(self):
        return 200

    def winfo_reqheight(self):
        return 200


def test_fit_window_uses_ctk_requested_logical_size(monkeypatch):
    window = _FakeWindow()
    monkeypatch.setattr(theme, "_screen_bounds", lambda _window: (0, 0, 1920, 1080))

    layout = theme.fit_window_to_screen(window)

    assert (layout.width, layout.height) == (620, 520)
    assert window.minimum_calls == [(520, 420)]
    assert window.geometry_calls == ["620x520+495+150"]


def test_main_layout_breakpoints_are_stable():
    assert main_layout_mode(MAIN_LAYOUT_COMPACT_MIN_WIDTH - 1) == "narrow"
    assert main_layout_mode(MAIN_LAYOUT_COMPACT_MIN_WIDTH) == "compact"
    assert main_layout_mode(MAIN_LAYOUT_WIDE_MIN_WIDTH - 1) == "compact"
    assert main_layout_mode(MAIN_LAYOUT_WIDE_MIN_WIDTH) == "wide"


def test_adaptive_tab_columns_wrap_all_destinations():
    assert adaptive_tab_columns(1120, 11) == 10
    assert adaptive_tab_columns(720, 11) == 6
    assert adaptive_tab_columns(480, 11) == 4
    assert adaptive_tab_columns(80, 11) == 1


def test_toast_wraplength_respects_physical_screen_and_dpi():
    assert _toast_wraplength(1920, 1.5) == 360
    assert _toast_wraplength(480, 1.5) == 256


def test_splash_layout_keeps_normal_size_and_fits_tiny_screen():
    assert _splash_layout(1920, 1080) == (380, 168, 770, 456)
    assert _splash_layout(320, 180) == (288, 148, 16, 16)


def test_large_two_pane_dialogs_stack_and_wrap_actions_on_narrow_screens():
    assert _git_history_layout(900) == (False, 5)
    assert _git_history_layout(720) == (True, 3)
    assert _git_history_layout(480) == (True, 2)

    assert _auto_continue_logs_layout(900) == (False, 5, False)
    assert _auto_continue_logs_layout(720) == (True, 5, True)
    assert _auto_continue_logs_layout(480) == (True, 3, True)
