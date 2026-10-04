"""Preset-entry layout/callback contracts using doubles only, never native Tk."""
from types import SimpleNamespace

import pytest

from ui.widgets import service_route_overview as overview_module


class Widget:
    def __init__(self, master=None, **options):
        self.master = master
        self.options = {"state": "normal", **options}
        self.width = 800
        self.scaling = 1.0
        self.grid_options = {}
        self.columns = {}
        self.grid_calls = []
        self.configure_calls = []
        self.bindings = {}

    def pack(self, **_options):
        pass

    def pack_forget(self):
        pass

    def bind(self, event, callback, **_options):
        self.bindings[event] = callback

    def cget(self, key):
        return self.options.get(key)

    def configure(self, **options):
        self.options.update(options)
        self.configure_calls.append(options)

    def grid(self, **options):
        self.grid_options.update(options)
        self.grid_calls.append(dict(options))

    def grid_forget(self):
        self.grid_options.clear()

    def grid_columnconfigure(self, column, **options):
        self.columns[column] = options

    def winfo_width(self):
        return self.width

    def _get_widget_scaling(self):
        return self.scaling

    def invoke(self):
        if self.options["state"] != "disabled":
            self.options["command"]()


@pytest.fixture
def build_overview(monkeypatch):
    base = overview_module.ctk.CTkFrame
    for name in ("__init__", "pack", "bind", "_get_widget_scaling"):
        monkeypatch.setattr(base, name, getattr(Widget, name))
    for name in ("CTkFrame", "CTkLabel", "CTkButton"):
        monkeypatch.setattr(overview_module.ctk, name, Widget)
    monkeypatch.setattr(overview_module, "font", lambda *_args: ("synthetic", 12))
    monkeypatch.setattr(overview_module, "button_style", lambda kind, **_kwargs: {"style": kind})
    monkeypatch.setattr(overview_module, "bind_wraplength", lambda *_args, **_kwargs: None)

    def build(*, preset=True, inspect=True):
        events = []
        widget = overview_module.ServiceRouteOverview(
            None, command=lambda service: events.append(("manage", service)),
            inspect_command=(lambda: events.append(("inspect",))) if inspect else None,
            preset_command=(lambda: events.append(("preview",))) if preset else None,
        )
        return widget, events

    return build


def resize(widget, logical_width, scale):
    widget.scaling = scale
    width = round(logical_width * scale)
    widget._header.width = width
    widget._layout_header_actions(SimpleNamespace(width=width))


def placement(button):
    return tuple(button.grid_options[key] for key in ("row", "column", "columnspan"))


def test_preset_is_visible_primary_and_only_opens_preview_on_explicit_click(build_overview):
    widget, events = build_overview()
    assert not events
    assert widget._preset.cget("text") == "一键套用智能分流方案"
    assert widget._preset.cget("style") == "primary"
    assert widget._manage.cget("style") == "secondary"
    assert widget._inspect.cget("style") == "secondary"
    note = widget._preset_note.cget("text")
    assert "AI 优先家宽" in note and "普通网站优先非家宽" in note
    assert "先预览再应用" in note and "不会自动修改" in note
    widget._preset.invoke()
    assert events == [("preview",)]
    widget._manage.invoke()
    widget._inspect.invoke()
    assert events == [("preview",), ("manage", ""), ("inspect",)]


def test_disabled_overview_blocks_preset_and_equal_state_does_not_redraw(build_overview):
    widget, events = build_overview()
    controls = (widget._preset, widget._manage, widget._inspect)
    widget.set_enabled(False)
    assert all(button.cget("state") == "disabled" for button in controls)
    for button in controls:
        button.invoke()
    widget._open_preset()
    assert not events
    calls = [len(button.configure_calls) for button in controls]
    widget.set_enabled(False)
    assert [len(button.configure_calls) for button in controls] == calls
    widget.set_enabled(True)
    assert all(button.cget("state") == "normal" for button in controls)
    widget._preset.invoke()
    assert events == [("preview",)]


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 1.75, 2.0, 2.5])
@pytest.mark.parametrize("width,columns,expected", [
    (220, 1, [(0, 0, 1), (1, 0, 1), (2, 0, 1)]),
    (279, 1, [(0, 0, 1), (1, 0, 1), (2, 0, 1)]),
    (280, 2, [(0, 0, 2), (1, 0, 1), (1, 1, 1)]),
    (499, 2, [(0, 0, 2), (1, 0, 1), (1, 1, 1)]),
    (500, 3, [(0, 0, 1), (0, 1, 1), (0, 2, 1)]),
    (950, 3, [(0, 0, 1), (0, 1, 1), (0, 2, 1)]),
])
def test_three_buttons_wrap_using_logical_width(build_overview, scale, width, columns, expected):
    widget, events = build_overview()
    resize(widget, width, scale)
    assert widget._header_action_columns == columns
    assert [placement(button) for button in (widget._preset, widget._manage, widget._inspect)] == expected
    assert not events


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0, 2.5])
def test_optional_preset_preserves_existing_two_button_breakpoint(build_overview, scale):
    widget, events = build_overview(preset=False)
    assert widget._preset is None and widget._preset_note is None
    resize(widget, 279, scale)
    assert widget._header_action_columns == 1
    assert placement(widget._inspect) == (1, 0, 1)
    resize(widget, 280, scale)
    assert widget._header_action_columns == 2
    assert placement(widget._inspect) == (0, 1, 1)
    widget._open_preset()
    widget.set_enabled(False)
    assert not events


def test_optional_inspector_still_has_usable_preset_and_manage_layout(build_overview):
    widget, _ = build_overview(inspect=False)
    resize(widget, 349, 2.5)
    assert placement(widget._preset) == (0, 0, 1)
    assert placement(widget._manage) == (1, 0, 1)
    resize(widget, 350, 2.5)
    assert placement(widget._manage) == (0, 1, 1)
    widget.set_enabled(False)
    assert widget._preset.cget("state") == "disabled"


def test_resize_roundtrips_reset_spans_padding_and_unused_columns(build_overview):
    widget, events = build_overview()
    buttons = (widget._preset, widget._manage, widget._inspect)
    resize(widget, 900, 1)
    original = [dict(button.grid_options) for button in buttons]
    for _ in range(3):
        for width, scale in ((420, 2.5), (220, 2), (500, 1.5), (900, 1)):
            resize(widget, width, scale)
        assert [button.grid_options for button in buttons] == original
    calls = [len(button.grid_calls) for button in buttons]
    resize(widget, 800, 1.25)
    assert [len(button.grid_calls) for button in buttons] == calls
    resize(widget, 220, 2.5)
    assert widget._header_actions.columns[1]["weight"] == 0
    assert widget._header_actions.columns[2]["weight"] == 0
    assert not events
