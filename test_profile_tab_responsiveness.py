"""Real Tk reuse checks plus deterministic refresh coalescing; isolated data only."""
from copy import deepcopy
import time
from types import SimpleNamespace

import customtkinter as ctk
import pytest

from models.profile import ClaudeAccountProfile, ClaudeProfile, CodexAccountProfile, CodexProfile
from ui.tabs import claude_tab, codex_tab


CASES = [(claude_tab, claude_tab.ClaudeTab, "claude"), (codex_tab, codex_tab.CodexTab, "codex")]


def payload(kind):
    profiles = [CodexProfile(f"API {i}") if kind == "codex" else
                ClaudeProfile(f"API {i}", "synthetic-ref", "https://example.invalid") for i in range(3)]
    accounts = [CodexAccountProfile(f"Account {i}", "synthetic-ref") if kind == "codex" else
                ClaudeAccountProfile(f"Account {i}", "synthetic-ref") for i in range(3)]
    return {
        "ok": True, "runtime": {}, "account_runtime": {},
        "profiles": [{"profile": p, "is_active": False, "auth_identity": "synthetic"} for p in profiles],
        "accounts": [{"profile": a, "is_active": False, "snapshot": (True, "synthetic")} for a in accounts],
    }


def drain(root, tab):
    deadline = time.monotonic() + 8
    while tab._card_render_pending() and time.monotonic() < deadline:
        root.update()
        time.sleep(0.004)
    root.update()
    assert not tab._card_render_pending()


@pytest.fixture(params=CASES, ids=["claude", "codex"])
def view(request, tk_root, monkeypatch):
    module, klass, kind = request.param
    active = [True]
    monkeypatch.setattr(module, "is_active_tab", lambda _: active[0])
    window = ctk.CTkToplevel(tk_root)
    window.geometry("760x650")
    tab = klass(window)
    tab.pack(fill="both", expand=True)
    tab.after_cancel(tab._initial_refresh_after_id)
    tab._initial_refresh_after_id = None
    tab._refresh_generation = 1
    data = payload(kind)
    tab._render_refresh_payload(data, 1)
    drain(tk_root, tab)
    try:
        yield tk_root, tab, data, active
    finally:
        window.destroy()
        tk_root.update()


def test_native_unchanged_refresh_and_tab_return_preserve_cards_and_scroll(view, monkeypatch):
    root, tab, data, _ = view
    before = [frame.winfo_children() for frame in (tab._cards_frame, tab._account_cards_frame)]
    tab._parent_canvas.yview_moveto(0.6)
    root.update()
    scroll = tab._parent_canvas.yview()
    tab._show_refresh_loading()
    tab._render_refresh_payload(deepcopy(data), 1)
    drain(root, tab)
    assert before == [frame.winfo_children() for frame in (tab._cards_frame, tab._account_cards_frame)]
    assert tab._parent_canvas.yview() == pytest.approx(scroll)
    refreshes = []
    monkeypatch.setattr(tab, "refresh", lambda: refreshes.append(True))
    tab._suspend_background_work()
    tab._resume_background_work()
    assert refreshes == [] and not tab._deferred_render_pending


def test_native_changes_replace_only_affected_cards_and_remove_invalid_auth_actions(view):
    root, tab, data, _ = view
    old_api, old_accounts = [frame.pack_slaves() for frame in (tab._cards_frame, tab._account_cards_frame)]
    data["profiles"][0]["is_active"] = True
    data["accounts"][1]["snapshot"] = (False, "synthetic invalid snapshot")
    tab._render_refresh_payload(data, 1)
    drain(root, tab)
    new_api, new_accounts = [frame.pack_slaves() for frame in (tab._cards_frame, tab._account_cards_frame)]
    assert not old_api[0].winfo_exists() and not old_accounts[1].winfo_exists()
    assert new_api[1:] == old_api[1:]
    assert new_accounts[0] is old_accounts[0] and new_accounts[2] is old_accounts[2]
    assert new_accounts[1]._on_switch is None
    assert new_api[0]._name == data["profiles"][0]["profile"].name


def test_stale_result_does_not_replace_latest_widgets(view):
    root, tab, data, _ = view
    old = tab._cards_frame.pack_slaves()
    data["profiles"] = []
    tab._render_refresh_payload(data, 0)
    root.update()
    assert tab._cards_frame.pack_slaves() == old
    assert not tab._card_render_pending()


def test_hidden_error_defers_refresh_and_completed_return_resumes_once(view, monkeypatch):
    _, tab, _, active = view
    active[0] = False
    tab._show_refresh_error("synthetic read failure")
    assert tab._deferred_render_pending and not tab._card_render_pending()
    calls = []
    monkeypatch.setattr(tab, "refresh", lambda: calls.append(True))
    active[0] = True
    tab._resume_background_work()
    tab._resume_background_work()
    assert calls == [True]


def test_cancel_partial_render_and_destroy_leave_no_owned_timers(view):
    _, tab, data, _ = view
    data["profiles"][0]["profile"].name = "Renamed"
    tab._render_refresh_payload(data, 1)
    assert tab._card_render_pending()
    tab._suspend_background_work()
    assert tab._deferred_render_pending and not tab._card_render_pending()
    renderers = tab._profile_card_lists
    tab.destroy()
    assert not any(renderer.pending for renderer in renderers)


@pytest.mark.parametrize("module,klass,kind", CASES)
def test_repeated_refreshes_use_one_worker_then_read_latest_state(monkeypatch, module, klass, kind):
    tab = object.__new__(klass)
    tab._cards_frame = tab._account_cards_frame = object()
    tab._refresh_generation = 0
    tab._cancel_profile_render = lambda: None
    tab._show_refresh_loading = lambda: None
    tab._is_alive = lambda: True
    rendered, errors, workers, completions = [], [], [], []
    tab._render_refresh_payload = lambda data, gen: rendered.append((data, gen))
    tab._show_refresh_error = errors.append
    monkeypatch.setattr(module, "threading", SimpleNamespace(
        Thread=lambda **args: SimpleNamespace(start=lambda: workers.append(args["target"])),
    ))
    monkeypatch.setattr(module, "run_on_ui_thread", lambda _tab, callback: completions.append(callback))
    current = ["old"]
    manager = SimpleNamespace(**{
        f"list_switchable_{kind}_profiles": lambda: [],
        f"list_{kind}_account_profiles": lambda: [],
        f"get_{kind}_runtime_summary": lambda: {"profile_name": current[0]},
        f"get_{kind}_account_runtime_summary": lambda: {},
    })
    monkeypatch.setattr(module, "profile_manager", manager)
    for _ in range(20):
        tab.refresh()
    assert len(workers) == 1
    workers.pop()()
    current[0] = "new"
    completions.pop()()
    assert not rendered and len(workers) == 1
    workers.pop()()
    completions.pop()()
    assert len(rendered) == 1 and rendered[0][0]["runtime"]["profile_name"] == "new"
    assert not errors and not tab._refresh_inflight and not tab._refresh_requested
