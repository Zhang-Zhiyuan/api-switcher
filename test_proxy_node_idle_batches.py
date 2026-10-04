"""Synthetic scheduling contracts: no native UI or real subscription data."""
import pytest

from ui.widgets.proxy_node_picker import ProxyNodePicker
from ui.widgets import proxy_node_picker as module


@pytest.fixture
def scheduled():
    picker = object.__new__(ProxyNodePicker)
    pending, events = {}, []

    def after(delay, callback):
        token = f"scheduled-{len(events)}"
        events.append((token, delay))
        pending[token] = callback
        return token

    picker.after = after
    picker.after_cancel = lambda token: pending.pop(token, None)
    return picker, pending, events


def test_idle_stage_only_queues_timer_without_rendering_reentrantly(scheduled):
    picker, pending, events = scheduled
    slot = "_checkbox_sync_after_id"
    painted = []
    token = picker._schedule_idle_batch(slot, lambda: painted.append(True))
    setattr(picker, slot, token)
    assert events == [(token, "idle")]
    pending.pop(token)()
    assert not painted
    timer_token = getattr(picker, slot)
    assert timer_token != token
    assert events[-1] == (timer_token, 0)
    pending.pop(timer_token)()
    assert painted == [True]


def test_stale_idle_stage_cannot_replace_a_newer_cancellation_token(scheduled):
    picker, pending, events = scheduled
    slot = "_checkbox_sync_after_id"
    token = picker._schedule_idle_batch(slot, lambda: None)
    setattr(picker, slot, "newer-batch")
    pending.pop(token)()
    assert getattr(picker, slot) == "newer-batch"
    assert len(events) == 1


@pytest.mark.parametrize("idle_finished", [False, True])
def test_checkbox_cancel_removes_whichever_scheduler_stage_is_pending(scheduled, idle_finished):
    picker, pending, _events = scheduled
    picker._checkbox_sync_generation = 0
    painted = []
    token = picker._schedule_idle_batch("_checkbox_sync_after_id", lambda: painted.append(True))
    picker._checkbox_sync_after_id = token
    if idle_finished:
        pending.pop(token)()
    picker._cancel_checkbox_sync()
    assert not pending and not painted
    assert picker._checkbox_sync_after_id is None
    assert picker._checkbox_sync_generation == 1


def test_timer_stage_failure_finishes_work_instead_of_leaving_loading_pending(scheduled):
    picker, pending, _events = scheduled
    painted = []
    token = picker._schedule_idle_batch("_checkbox_sync_after_id", lambda: painted.append(True))
    picker._checkbox_sync_after_id = token

    def unavailable(*_args):
        raise RuntimeError("synthetic scheduling failure")

    picker.after = unavailable
    pending.pop(token)()
    assert picker._checkbox_sync_after_id is None
    assert painted == [True]


def test_native_row_creation_keeps_ordinary_timer_scheduling(scheduled, monkeypatch):
    picker, _pending, events = scheduled
    picker._render_generation = 1
    picker._list_frame = object()
    picker._render_plan_item = lambda *_args: None
    monkeypatch.setattr(module, "is_active_tab", lambda _widget: True)
    monkeypatch.setattr(module, "recent_user_scroll", lambda *_args, **_kwargs: False)
    times = iter([0.0, 0.02])
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(times))
    picker._render_plan_batch(1, [("row", "first", None), ("row", "second", None)], 0)
    assert events == [(picker._render_batch_after_id, picker.RENDER_BATCH_DELAY_MS)]
