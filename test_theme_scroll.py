from __future__ import annotations

from types import SimpleNamespace

from ui import theme


class _Widget:
    def __init__(self, master=None, parent_canvas=None):
        self.master = master
        if parent_canvas is not None:
            self._parent_canvas = parent_canvas


class _Scrollable:
    def __init__(self, y=(0.0, 1.0), x=(0.0, 1.0)):
        self._y = y
        self._x = x
        self.calls = []

    def yview(self, *args):
        if args:
            self.calls.append(("y", args))
            return None
        return self._y

    def xview(self, *args):
        if args:
            self.calls.append(("x", args))
            return None
        return self._x


class _WrapContainer:
    def __init__(self, width=640):
        self.width = width
        self.bindings = {}
        self.idle_callbacks = []

    def bind(self, event_name, callback, add=None):
        self.bindings[event_name] = callback

    def after_idle(self, callback):
        self.idle_callbacks.append(callback)
        return f"idle-{len(self.idle_callbacks)}"

    def winfo_width(self):
        return self.width


class _WrapLabel:
    def __init__(self):
        self.exists = True
        self.configures = []

    def winfo_exists(self):
        return self.exists

    def configure(self, **kwargs):
        self.configures.append(kwargs)


def test_event_scroll_chain_collects_nested_scroll_canvases_once():
    outer_canvas = object()
    inner_canvas = object()
    root = _Widget(parent_canvas=outer_canvas)
    inner = _Widget(master=root, parent_canvas=inner_canvas)
    leaf = _Widget(master=inner)
    event = SimpleNamespace(widget=leaf)

    chain = theme._event_scroll_chain(event)

    assert chain == (inner_canvas, outer_canvas)

    leaf.master = None
    assert theme._event_scroll_chain(event) is chain


def test_bind_wraplength_coalesces_configure_updates_until_idle():
    container = _WrapContainer(width=700)
    label = _WrapLabel()

    theme.bind_wraplength(container, label, padding=40, min_width=220, max_width=900)

    assert len(container.idle_callbacks) == 1
    assert label.configures == []

    container.bindings["<Configure>"]()
    container.bindings["<Configure>"]()
    assert len(container.idle_callbacks) == 1

    container.idle_callbacks.pop(0)()
    assert label.configures == [{"wraplength": 660}]

    container.width = 710
    container.bindings["<Configure>"]()
    container.idle_callbacks.pop(0)()
    assert label.configures[-1] == {"wraplength": 670}


def test_wheel_delta_handles_touchpad_and_malformed_events():
    assert theme._wheel_direction(SimpleNamespace(delta=0.5, num=0)) == 1
    assert theme._wheel_direction(SimpleNamespace(delta=-0.5, num=0)) == -1
    assert theme._wheel_direction(SimpleNamespace(delta="bad", num=4)) == 1
    assert theme._wheel_direction(SimpleNamespace(delta="bad", num=5)) == -1
    assert theme._wheel_direction(SimpleNamespace(delta="bad", num="bad")) == 0


def test_event_scroll_consumed_marker_is_reusable_across_handlers():
    event = SimpleNamespace()

    assert not theme._event_scroll_consumed(event)

    theme._mark_event_scroll_consumed(event)

    assert theme._event_scroll_consumed(event)


def test_scroll_widget_can_consume_only_when_direction_has_room():
    event_up = SimpleNamespace(delta=120, num=0)
    event_down = SimpleNamespace(delta=-120, num=0)

    assert not theme._scroll_widget_can_consume(_Scrollable(y=(0.0, 0.4)), event_up)
    assert theme._scroll_widget_can_consume(_Scrollable(y=(0.2, 0.6)), event_up)
    assert theme._scroll_widget_can_consume(_Scrollable(y=(0.2, 0.6)), event_down)
    assert not theme._scroll_widget_can_consume(_Scrollable(y=(0.6, 1.0)), event_down)
    assert not theme._scroll_widget_can_consume(_Scrollable(y=(0.0, 1.0)), event_down)


def test_scroll_widget_uses_minimum_units_for_small_windows_delta():
    event = SimpleNamespace(delta=0.5, num=0)
    scrollable = _Scrollable(y=(0.2, 0.8))

    assert theme._scroll_widget(scrollable, event)

    assert scrollable.calls == [("y", ("scroll", -1, "units"))]


def test_scroll_units_use_slightly_higher_windows_sensitivity(monkeypatch):
    monkeypatch.setattr(theme.sys, "platform", "win32")

    assert theme._scroll_units(SimpleNamespace(delta=120, num=0)) == -24
    assert theme._scroll_units(SimpleNamespace(delta=-120, num=0)) == 24
    assert theme._scroll_units(SimpleNamespace(delta=60, num=0)) == -24
    assert theme._scroll_units(SimpleNamespace(delta=-60, num=0)) == 24
    assert theme._scroll_units(SimpleNamespace(delta=5, num=0)) == -1
    assert theme._scroll_units(SimpleNamespace(delta=-5, num=0)) == 1


def test_scroll_widget_supports_horizontal_direction():
    event = SimpleNamespace(delta=-0.5, num=0)
    scrollable = _Scrollable(y=(0.0, 1.0), x=(0.2, 0.8))

    assert theme._scroll_widget(scrollable, event, horizontal=True)

    assert scrollable.calls == [("x", ("scroll", 1, "units"))]
