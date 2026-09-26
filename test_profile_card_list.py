"""Deterministic render lifecycle checks; synthetic profiles, no real auth."""
from itertools import permutations

import pytest

from models.profile import CodexProfile
from ui.widgets.profile_card_list import ProfileCardList


class Frame:
    def __init__(self):
        self.children = []
        self.order = []

    def pack_slaves(self):
        return self.order[:]

    def winfo_children(self):
        return self.children[:]


class Card:
    def __init__(self, frame, name):
        self.frame, self.name = frame, name
        self.destroyed = False
        self.pack_calls = 0
        frame.children.append(self)

    def pack(self, **options):
        self.pack_calls += 1
        if self in self.frame.order:
            self.frame.order.remove(self)
        before = options.get("before")
        index = self.frame.order.index(before) if before else len(self.frame.order)
        self.frame.order.insert(index, self)

    def destroy(self):
        self.destroyed = True
        self.frame.children.remove(self)
        if self in self.frame.order:
            self.frame.order.remove(self)


class Owner:
    def __init__(self):
        self.callbacks = {}
        self.counter = 0

    def after(self, delay, callback):
        assert delay >= 1
        self.counter += 1
        self.callbacks[self.counter] = callback
        return self.counter

    def after_cancel(self, handle):
        self.callbacks.pop(handle, None)

    def after_idle(self, callback):
        return self.after(1, callback)

    def step(self):
        self.callbacks.pop(next(iter(self.callbacks)))()

    def drain(self):
        for _ in range(200):
            if not self.callbacks:
                return
            self.step()
        raise AssertionError("render failed to finish")


def item(name, **values):
    return {"profile": CodexProfile(name), "is_active": False, **values}


@pytest.fixture
def listing():
    frame, owner, errors = Frame(), Owner(), []
    renderer = ProfileCardList(owner, frame, is_active=lambda: True, on_error=lambda: errors.append(True))
    created = []

    def create(data):
        created.append(data["profile"].name)
        return Card(frame, data["profile"].name)

    def render(items):
        renderer.render(items, create, lambda: Card(frame, "empty"))

    return renderer, render, owner, frame, created, errors


def test_identical_refresh_reuses_widgets_without_scheduling(listing):
    renderer, render, owner, frame, created, _ = listing
    render([item("A"), item("B")])
    owner.drain()
    before = frame.order[:]
    assert not renderer.pending
    render([item("A"), item("B")])
    assert not renderer.pending and not owner.callbacks
    assert frame.order == before and created == ["A", "B"]


@pytest.mark.parametrize("field,value", [("is_active", True), ("snapshot", (False, "expired")),
                                          ("auth_identity", "new identity")])
def test_one_changed_card_does_not_rebuild_others(listing, field, value):
    _, render, owner, frame, created, _ = listing
    first, second = item("A"), item("B")
    render([first, second])
    owner.drain()
    before = frame.order[:]
    first[field] = value
    render([first, second])
    owner.drain()
    assert before[0].destroyed and not before[1].destroyed
    assert frame.order[1] is before[1]
    assert created == ["A", "B", "A"]
    assert before[1].pack_calls == 1  # Inserting A naturally shifts B back.


def test_in_place_profile_edit_and_rename_are_not_hidden_by_cache(listing):
    _, render, owner, frame, created, _ = listing
    data = item("A")
    render([data])
    owner.drain()
    data["profile"].model = "synthetic-new-model"
    render([data])
    owner.drain()
    data["profile"].name = "Renamed"
    render([data])
    owner.drain()
    assert created == ["A", "A", "Renamed"]
    assert [card.name for card in frame.order] == ["Renamed"]


def test_all_small_reorders_keep_correct_order_and_reuse(listing):
    renderer, render, owner, frame, created, _ = listing
    for names in permutations("ABCD"):
        render([item(name) for name in names])
        owner.drain()
        assert [card.name for card in frame.order] == list(names)
        assert not renderer.pending
    assert len(created) == 4


def test_insert_remove_reorder_and_empty_transitions(listing):
    _, render, owner, frame, _, _ = listing
    for names in ("ABC", "XACB", "CA", "YAZ", "", "Q", "Q", ""):
        render([item(name) for name in names])
        owner.drain()
        assert [card.name for card in frame.order] == (list(names) or ["empty"])
        assert len(frame.children) == len(frame.order)


def test_partial_refresh_reuses_completed_work_and_cancels_stale_callback(listing):
    renderer, render, owner, frame, created, _ = listing
    render([item("A"), item("B"), item("C")])
    assert created == []  # Even the first card waits for the event loop.
    owner.step()
    assert created == ["A"]
    stale = next(iter(owner.callbacks.values()))
    renderer.cancel()
    assert not renderer.pending
    render([item("A"), item("D")])
    token = renderer._after_id
    stale()
    assert renderer._after_id == token
    owner.drain()
    assert created == ["A", "D"]
    assert [card.name for card in frame.order] == ["A", "D"]


def test_delete_work_is_bounded_to_one_card_per_tick(listing):
    _, render, owner, frame, _, _ = listing
    render([item(str(i)) for i in range(40)])
    owner.drain()
    render([])
    assert len(frame.children) == 40
    last = frame.order[-1]
    owner.step()
    assert len(frame.children) == 39
    assert last.destroyed
    owner.drain()
    assert [card.name for card in frame.order] == ["empty"]


def test_error_empty_recovery_and_failed_factory_retry(listing):
    renderer, render, owner, frame, _, errors = listing
    render([])
    owner.drain()
    renderer.show_message("unreadable", lambda: Card(frame, "error"))
    owner.drain()
    assert [card.name for card in frame.order] == ["error"]

    def fail(_item):
        Card(frame, "unpacked partial card")
        raise RuntimeError("synthetic widget error")

    renderer.render([item("A")], fail, lambda: None)
    owner.drain()
    assert errors == [True] and "A" not in renderer.rows
    render([item("A")])
    owner.drain()
    assert [card.name for card in frame.children] == ["A"]


def test_hidden_callback_does_not_render(listing):
    renderer, render, owner, frame, _, _ = listing
    render([item("A")])
    renderer.is_active = lambda: False
    owner.drain()
    assert not frame.children and not renderer.pending
    renderer.is_active = lambda: True
    render([item("A")])
    owner.drain()
    assert len(frame.children) == 1


def test_scrolling_pauses_render_without_losing_remaining_cards(listing):
    renderer, render, owner, frame, created, _ = listing
    renderer.should_pause = lambda: True
    render([item("A"), item("B")])
    for _ in range(5):
        owner.step()
    assert not created and renderer.pending
    renderer.should_pause = lambda: False
    owner.drain()
    assert [card.name for card in frame.order] == ["A", "B"]
    assert not renderer.pending


def test_completion_runs_once_for_noop_or_success_never_cancelled_plan(listing):
    renderer, render, owner, frame, _, _ = listing
    done = []
    def show(name):
        renderer.render([item(name)], lambda data: Card(frame, data["profile"].name), lambda: None,
                        on_complete=lambda: done.append(name))
    show("A")
    assert not done
    owner.drain()
    assert done == ["A"]
    show("A")
    assert done == ["A", "A"] and not renderer.pending
    show("B")
    owner.step()
    stale = next(iter(owner.callbacks.values()))
    render([item("C")])
    stale()
    owner.drain()
    assert done == ["A", "A"]
    assert [card.name for card in frame.order] == ["C"]
