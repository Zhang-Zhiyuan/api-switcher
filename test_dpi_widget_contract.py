"""Synthetic responsive-layout contracts; no Tk root or saved user state."""
from types import SimpleNamespace

import pytest

from ui import theme
from ui.widgets.auto_continue_control import _auto_continue_layout, _bind_responsive_grid
from ui.widgets.profile_card import _bind_profile_card_action_grid, _profile_card_action_columns


class Frame:
    def __init__(self, width, scale):
        self.width = width
        self.scale = scale
        self.bindings = {}
        self.columns = {}

    def winfo_width(self):
        return self.width

    def _get_widget_scaling(self):
        return self.scale

    def bind(self, sequence, callback, **_kwargs):
        self.bindings[sequence] = callback

    def grid_columnconfigure(self, column, **kwargs):
        self.columns[column] = kwargs

    def resize(self, *, width=None, scale=None):
        if width is not None:
            self.width = width
        if scale is not None:
            self.scale = scale
        self.bindings["<Configure>"](SimpleNamespace(width=self.width))


class Control:
    def __init__(self):
        self.placements = []

    def grid(self, **kwargs):
        self.placements.append(kwargs)


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 1.75, 2.0, 2.5])
@pytest.mark.parametrize("logical_width", [300, 480, 600, 800])
@pytest.mark.parametrize("section,count", [(0, 3), (1, 7), (2, 3)])
def test_auto_continue_grid_uses_logical_width_at_every_scale(scale, logical_width, section, count):
    frame = Frame(round(logical_width * scale), scale)
    controls = [Control() for _ in range(count)]
    _bind_responsive_grid(frame, controls, lambda width: _auto_continue_layout(width)[section])
    columns = min(count, _auto_continue_layout(logical_width)[section])
    assert all(control.placements for control in controls)
    for index, control in enumerate(controls):
        assert control.placements[-1]["column"] == index % columns
        assert control.placements[-1]["row"] == index // columns


def test_auto_continue_grid_reflows_when_scale_changes_without_physical_resize():
    frame = Frame(840, 1.0)
    controls = [Control() for _ in range(7)]
    _bind_responsive_grid(frame, controls, lambda width: _auto_continue_layout(width)[1])
    assert controls[-1].placements[-1]["column"] == 6
    for scale, columns in ((2.0, 3), (2.5, 2), (1.0, 7)):
        frame.resize(scale=scale)
        for index, control in enumerate(controls):
            assert control.placements[-1]["column"] == index % columns
            assert control.placements[-1]["row"] == index // columns
        assert all(frame.columns[column]["weight"] == 0 for column in range(columns, 7))


def test_auto_continue_grid_keeps_same_layout_when_physical_width_tracks_scale():
    frame = Frame(480, 1.0)
    controls = [Control() for _ in range(7)]
    _bind_responsive_grid(frame, controls, lambda width: _auto_continue_layout(width)[1])
    for scale in (1.25, 1.5, 1.75, 2.0, 2.5):
        frame.resize(width=round(480 * scale), scale=scale)
    assert all(len(control.placements) == 1 for control in controls)


def test_auto_continue_grid_handles_a_non_ctk_container():
    frame = Frame(480, 1.0)
    frame._get_widget_scaling = lambda: (_ for _ in ()).throw(AttributeError("no CTk scaling"))
    controls = [Control() for _ in range(7)]
    _bind_responsive_grid(frame, controls, lambda width: _auto_continue_layout(width)[1])
    assert controls[-1].placements[-1]["row"] == 2
    assert controls[-1].placements[-1]["column"] == 0


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 1.75, 2.0, 2.5])
@pytest.mark.parametrize("mapped", [False, True])
def test_profile_actions_use_logical_ancestor_padding_only_before_mapping(scale, mapped):
    logical_width = 300
    frame = Frame(round(logical_width * scale) if mapped else 1, scale)
    frame.master = Frame(round(logical_width * scale), scale)
    controls = [Control() for _ in range(6)]
    _bind_profile_card_action_grid(frame, controls)
    columns = _profile_card_action_columns(logical_width if mapped else logical_width - 28, len(controls))
    for index, control in enumerate(controls):
        assert control.placements[-1]["column"] == index % columns
        assert control.placements[-1]["row"] == index // columns


class WrapWidget:
    """Per-widget bindings: child Configure does not bubble to a parent frame."""

    def __init__(self, width=600, scale=1.5):
        self.width = width
        self.scale = scale
        self._canvas, self._label = object(), object()
        self.bindings = {}
        self.pending = {}
        self.cancelled = []
        self.configures = []
        self.exists = True
        self.reads = 0
        self.serial = 0

    def bind(self, sequence, callback, add=None):
        assert add == "+"
        self.bindings.setdefault(sequence, []).append(callback)

    def emit(self, sequence, source=None):
        for callback in self.bindings.get(sequence, ()):
            callback(SimpleNamespace(widget=self._canvas if source is None else source))

    def after_idle(self, callback):
        self.serial += 1
        token = f"idle-{self.serial}"
        self.pending[token] = callback
        return token

    def after_cancel(self, token):
        self.cancelled.append(token)
        self.pending.pop(token, None)

    def drain(self):
        count = 0
        while self.pending:
            count += 1
            assert count < 10, "wraplength Configure recursively scheduled work"
            self.pending.pop(next(iter(self.pending)))()

    def winfo_exists(self):
        self.reads += 1
        return self.exists

    def winfo_width(self):
        self.reads += 1
        return self.width

    def _get_widget_scaling(self):
        return self.scale

    def configure(self, **kwargs):
        self.configures.append(kwargs)
        self.emit("<Configure>", self._label)


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.75, 2.0, 2.25, 2.5])
def test_wraplength_observes_label_scaling_without_parent_configure(scale):
    container, label = WrapWidget(), WrapWidget()
    theme.bind_wraplength(container, label, padding=10)
    container.drain()
    assert label.configures == [{"wraplength": 390}]
    assert not container.pending
    label.scale = scale
    label.emit("<Configure>", label._label)
    container.drain()
    assert label.configures[-1] == {"wraplength": round(600 / scale) - 10}
    assert not container.pending


def test_wraplength_label_height_events_do_not_add_idle_work_without_scaling_change():
    container, label = WrapWidget(), WrapWidget()
    theme.bind_wraplength(container, label, padding=10)
    container.drain()
    for _ in range(100):
        label.emit("<Configure>", label._label)
    assert not container.pending
    assert label.configures == [{"wraplength": 390}]


def test_wraplength_parent_and_label_scaling_events_share_one_idle_update():
    container, label = WrapWidget(), WrapWidget()
    theme.bind_wraplength(container, label, padding=10)
    container.drain()
    container.width = 675
    label.scale = 2.25
    for _ in range(10):
        container.emit("<Configure>")
        label.emit("<Configure>", label._label)
    assert len(container.pending) == 1
    container.drain()
    assert label.configures[-1] == {"wraplength": 290}
    assert not container.pending


@pytest.mark.parametrize("destroyed", ["container", "label"])
def test_wraplength_destruction_cancels_idle_and_late_callbacks_do_not_read_tcl(destroyed):
    container, label = WrapWidget(), WrapWidget()
    theme.bind_wraplength(container, label, padding=10)
    queued = next(iter(container.pending.values()))
    target = container if destroyed == "container" else label
    target.exists = False
    target.emit("<Destroy>")
    reads = container.reads + label.reads
    queued()  # A callback already taken from Tk's queue must also be harmless.
    container.emit("<Configure>")
    label.emit("<Configure>", label._label)
    assert not container.pending
    assert container.cancelled == ["idle-1"]
    assert container.reads + label.reads == reads
    assert not label.configures


def test_wraplength_ignores_unrelated_descendant_destroy_events():
    container, label = WrapWidget(), WrapWidget()
    theme.bind_wraplength(container, label, padding=10)
    container.emit("<Destroy>", object())
    container.drain()
    assert label.configures == [{"wraplength": 390}]


def test_wraplength_self_container_binds_configure_and_destroy_only_once():
    label = WrapWidget()
    theme.bind_wraplength(label, label, padding=10)
    assert len(label.bindings["<Configure>"]) == 1
    assert len(label.bindings["<Destroy>"]) == 1
    label.drain()
    label.scale = 2.25
    label.emit("<Configure>", label._label)
    label.drain()
    assert label.configures[-1] == {"wraplength": 257}
