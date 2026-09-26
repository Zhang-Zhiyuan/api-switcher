"""Real Tk geometry regressions, with all startup/deployment actions disabled."""
import time

import customtkinter as ctk
import pytest

from ui.app import App, global_action_columns
from ui.tabs.session_migration_tab import SessionMigrationTab


def settle(root):
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)


@pytest.fixture(scope="module")
def tk_root():
    # Override the shared CTk fixture: the App is this module's one interpreter.
    with pytest.MonkeyPatch.context() as patch:
        for name in ("API_SWITCHER_PRELOAD_TABS", "API_SWITCHER_WARM_TABS"):
            patch.setenv(name, "0")
        for name in (
            "_start_tray_icon", "_auto_start_local_proxy", "_schedule_local_proxy_watchdog",
            "_restore_subscription_timers", "_schedule_initial_tab_load", "_load_quick_switch_profiles",
        ):
            patch.setattr(App, name, lambda *_args, **_kwargs: None)
        root = App()
        root._layout_test_errors = []
        root.report_callback_exception = lambda *error: root._layout_test_errors.append(error)
        try:
            yield root
            assert not root._layout_test_errors
        finally:
            ctk.set_widget_scaling(1)
            root._exit_requested = True
            root.destroy()


def assert_inside(widget, parent):
    assert widget.winfo_viewable()
    assert widget.winfo_rootx() >= parent.winfo_rootx() - 2
    assert widget.winfo_rootx() + widget.winfo_width() <= parent.winfo_rootx() + parent.winfo_width() + 2


@pytest.mark.parametrize("scale", [1, 1.5])
@pytest.mark.parametrize("geometry", ["1120x800", "740x720", "480x600"])
def test_toolbar_and_feedback_remain_visible_at_compact_and_high_dpi_sizes(tk_root, geometry, scale):
    ctk.set_widget_scaling(scale)
    tk_root.geometry(geometry)
    settle(tk_root)
    columns = global_action_columns(tk_root._logical_main_width())
    assert len({button.grid_info()["column"] for button in tk_root._global_action_buttons}) == columns
    assert bool(tk_root._brand_header.winfo_ismapped()) == (columns == 4)
    for button in tk_root._global_action_buttons:
        assert_inside(button, tk_root)
        assert button._text_label.winfo_reqwidth() <= button.winfo_width()
    status = tk_root._status
    assert_inside(status, tk_root)
    assert status.winfo_height() >= status.winfo_reqheight()
    assert status.winfo_rooty() + status.winfo_height() <= tk_root.winfo_rooty() + tk_root.winfo_height()
    assert tk_root._tabview.winfo_height() > 30


@pytest.mark.parametrize("scale", [1, 1.5])
def test_session_stats_have_their_own_wrapped_row_after_repeated_resizes(tk_root, scale, monkeypatch):
    ctk.set_widget_scaling(scale)
    monkeypatch.setattr(SessionMigrationTab, "_schedule_initial_refresh", lambda _self: None)
    window = ctk.CTkToplevel(tk_root)
    tab = SessionMigrationTab(window)
    tab.pack(fill="both", expand=True)
    tab._stats_label.configure(text="会话 999999 | 已选 999999 | 已选主文件 999.99 GB | 全部 999.99 GB")
    try:
        for geometry in ("1280x800", "520x700", "1120x800", "740x720"):
            window.geometry(geometry)
            settle(tk_root)
            controls = [*tab._filter_groups, tab._select_visible_button, tab._clear_selection_button]
            for widget in [*controls, tab._stats_label]:
                assert_inside(widget, tab._filter_bar)
            assert tab._stats_label.winfo_rooty() >= max(
                widget.winfo_rooty() + widget.winfo_height() for widget in controls
            )
            assert tab._stats_label.winfo_height() >= tab._stats_label._label.winfo_reqheight()
    finally:
        window.destroy()
        ctk.set_widget_scaling(1)
        tk_root.update()
