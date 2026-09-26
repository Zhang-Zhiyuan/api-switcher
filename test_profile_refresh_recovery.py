"""Refresh completion failures must not leave account/API pages permanently busy."""
from types import SimpleNamespace
import threading

import pytest

from ui.tabs import claude_tab, codex_tab


@pytest.fixture(params=[(claude_tab, claude_tab.ClaudeTab, "claude"),
                       (codex_tab, codex_tab.CodexTab, "codex")], ids=["claude", "codex"])
def refresh_lab(request, monkeypatch):
    module, klass, kind = request.param
    tab = object.__new__(klass)
    tab._cards_frame = tab._account_cards_frame = object()
    tab._refresh_generation = 0
    tab._destroyed = False
    tab._cancel_profile_render = lambda: None
    tab._show_refresh_loading = lambda: None
    tab._is_alive = lambda: not tab._destroyed
    workers, callbacks, rendered, errors = [], [], [], []
    accepted = [False]
    tab._render_refresh_payload = lambda data, gen: rendered.append(gen)
    tab._show_refresh_error = errors.append
    monkeypatch.setattr(module, "threading", SimpleNamespace(
        Thread=lambda **args: SimpleNamespace(start=lambda: workers.append(args["target"])),
    ))
    monkeypatch.setattr(module, "profile_manager", SimpleNamespace(**{
        f"list_switchable_{kind}_profiles": lambda: [],
        f"list_{kind}_account_profiles": lambda: [],
        f"get_{kind}_runtime_summary": lambda: {},
        f"get_{kind}_account_runtime_summary": lambda: {},
    }))

    def dispatch(_tab, callback):
        callbacks.append(callback)
        return accepted[0]

    monkeypatch.setattr(module, "run_on_ui_thread", dispatch)
    tab._test_module = module
    return tab, workers, callbacks, rendered, errors, accepted


@pytest.mark.parametrize("extra_requests", [0, 12])
def test_rejected_completion_allows_retry_without_touching_tk(refresh_lab, extra_requests):
    tab, workers, callbacks, rendered, errors, accepted = refresh_lab
    tab.refresh()
    for _ in range(extra_requests):
        tab.refresh()
    assert len(workers) == 1
    workers.pop(0)()
    assert not tab._refresh_inflight
    assert tab._deferred_render_pending
    assert rendered == errors == []

    accepted[0] = True
    tab.refresh()
    assert len(workers) == 1
    workers.pop(0)()
    callbacks.pop()()
    assert not tab._refresh_inflight
    assert rendered == [tab._refresh_generation]


def test_late_rejected_callback_cannot_clear_a_new_refresh(refresh_lab):
    tab, workers, callbacks, rendered, errors, accepted = refresh_lab
    tab.refresh()
    workers.pop(0)()
    late = callbacks.pop()
    accepted[0] = True
    tab.refresh()
    assert len(workers) == 1
    late()
    assert tab._refresh_inflight
    assert not rendered and not errors
    workers.pop(0)()
    completion = callbacks.pop()
    completion()
    completion()  # A completion is consumed only once.
    assert rendered == [tab._refresh_generation]
    assert not tab._refresh_inflight


def test_destroyed_tab_does_not_launch_new_refresh(refresh_lab):
    tab, workers, callbacks, rendered, errors, _ = refresh_lab
    tab._destroyed = True
    tab.refresh()
    assert workers == callbacks == rendered == errors == []


def test_real_worker_recovers_from_dispatch_refusal_without_native_calls(refresh_lab, monkeypatch):
    tab, _, callbacks, rendered, errors, _ = refresh_lab
    threads = []

    def thread_factory(**kwargs):
        thread = threading.Thread(**kwargs)
        threads.append(thread)
        return thread

    def forbidden(*_args, **_kwargs):
        raise AssertionError("background worker must not touch Tk")

    monkeypatch.setattr(tab._test_module, "threading", SimpleNamespace(Thread=thread_factory))
    tab.after = tab.winfo_exists = tab.winfo_toplevel = forbidden
    tab.refresh()
    assert len(threads) == 1
    threads[0].join(timeout=2)
    assert not threads[0].is_alive()
    assert not tab._refresh_inflight and tab._deferred_render_pending
    assert len(callbacks) == 1 and not rendered and not errors
