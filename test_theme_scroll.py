from __future__ import annotations

from types import SimpleNamespace

from ui import theme


class _Widget:
    def __init__(self, master=None, parent_canvas=None):
        self.master = master
        if parent_canvas is not None:
            self._parent_canvas = parent_canvas


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
