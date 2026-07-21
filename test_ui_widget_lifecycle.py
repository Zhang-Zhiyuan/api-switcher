from __future__ import annotations

from types import SimpleNamespace

from ui.tabs.session_migration_tab import SessionMigrationTab
from ui.widgets.adaptive_tab_bar import AdaptiveTabBar
from ui.widgets.search_bar import SearchBar


def test_session_refresh_coalesces_request_while_scan_is_running(monkeypatch):
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._initial_refresh_after_id = None
    tab._deferred_refresh_pending = False
    tab._cards_frame = object()
    tab._refresh_in_progress = True
    tab._refresh_requested = False

    monkeypatch.setattr("ui.tabs.session_migration_tab.is_active_tab", lambda _widget: True)
    monkeypatch.setattr(
        "ui.tabs.session_migration_tab.recent_user_scroll",
        lambda *_args, **_kwargs: False,
    )

    SessionMigrationTab.refresh(tab)

    assert tab._refresh_in_progress is True
    assert tab._refresh_requested is True


def test_session_refresh_replays_latest_request_after_scan_finishes():
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._refresh_generation = 1
    tab._refresh_in_progress = True
    tab._refresh_requested = True
    replayed = []
    tab.refresh = lambda: replayed.append(True)

    SessionMigrationTab._finish_refresh(
        tab,
        1,
        {"records": [], "error": None},
    )

    assert tab._refresh_in_progress is False
    assert tab._refresh_requested is False
    assert replayed == [True]


def test_stale_session_refresh_callback_does_not_unlock_current_scan():
    tab = object.__new__(SessionMigrationTab)
    tab._destroyed = False
    tab._refresh_generation = 2
    tab._refresh_in_progress = True
    tab._refresh_requested = True

    SessionMigrationTab._finish_refresh(
        tab,
        1,
        {"records": [], "error": None},
    )

    assert tab._refresh_in_progress is True
    assert tab._refresh_requested is True


def test_search_enter_cancels_pending_debounced_callback():
    bar = object.__new__(SearchBar)
    bar._search_after_id = "after#1"
    bar._search_history = []
    bar.search_entry = SimpleNamespace(get=lambda: " query ")
    cancelled = []
    searches = []
    bar.after_cancel = cancelled.append
    bar.on_search = searches.append

    SearchBar._on_enter(bar, None)

    assert cancelled == ["after#1"]
    assert bar._search_after_id is None
    assert searches == ["query"]
    assert bar.get_history() == ["query"]


def test_adaptive_tab_layout_scheduling_is_coalesced_and_cancellable():
    bar = object.__new__(AdaptiveTabBar)
    bar._destroyed = False
    bar._layout_after_id = None
    scheduled = []
    cancelled = []

    def after_idle(callback):
        scheduled.append(callback)
        return "after#2"

    bar.after_idle = after_idle
    bar.after_cancel = cancelled.append

    AdaptiveTabBar._schedule_layout(bar)
    AdaptiveTabBar._schedule_layout(bar)

    assert len(scheduled) == 1
    assert bar._layout_after_id == "after#2"

    AdaptiveTabBar._cancel_pending_layout(bar)

    assert cancelled == ["after#2"]
    assert bar._layout_after_id is None


def test_adaptive_tab_layout_callback_is_noop_after_destroy():
    bar = object.__new__(AdaptiveTabBar)
    bar._destroyed = True
    bar._layout_after_id = "after#3"
    bar._buttons = {"unexpected": object()}

    AdaptiveTabBar._layout_buttons(bar)

    assert bar._layout_after_id is None
