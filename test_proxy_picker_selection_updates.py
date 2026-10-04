"""Pure redraw contracts: no Tk root, live nodes, or external configuration."""
from types import SimpleNamespace

import pytest

from ui.widgets import proxy_node_picker as module


class Control:
    def __init__(self):
        self.updates = []

    def configure(self, **kwargs):
        self.updates.append(kwargs)

    def pack(self, **_kwargs):
        pass


@pytest.fixture
def picker(monkeypatch):
    view = object.__new__(module.ProxyNodePicker)
    view._nodes = [SimpleNamespace(key=str(index)) for index in range(300)]
    view._node_key = lambda item: item.key
    view._node_scope_keys = {item.key for item in view._nodes}
    view._selected_key = "0"
    view._checked_keys = set()
    view._enabled = True
    view._visible_node_rows = {}
    view._row_cache = {}
    view._visible_checkboxes = {}
    view._on_select = None
    for item in view._nodes:
        row, button, checkbox = Control(), Control(), Control()
        view._visible_node_rows[item.key] = (row, button)
        view._row_cache[item.key] = {
            "row": row, "button": button, "checkbox": checkbox,
            "variable": SimpleNamespace(get=lambda: False),
            "labels": tuple(Control() for _ in range(4)),
            "presentation": (item.key, "node", "detail", "untested", "gray", "quality", "gray"),
            "selected": item.key == "0", "enabled": True,
        }
    monkeypatch.setattr(module, "button_style", lambda kind, **_kwargs: {
        "font": object(), "height": 28, "corner_radius": 6,
        "fg_color": kind, "hover_color": kind + "_hover", "text_color": "text",
    })
    return view


@pytest.mark.parametrize("method", ["select", "reuse"])
def test_selection_only_changes_button_text_and_palette(picker, method):
    if method == "select":
        picker._select("1")
        changed = ("0", "1")
    else:
        picker._selected_key = "1"
        cached = picker._row_cache["1"]
        picker._reuse_row(cached["presentation"], relayout=False)
        changed = ("1",)

    for key in changed:
        cached = picker._row_cache[key]
        assert len(cached["row"].updates) == 1
        assert len(cached["button"].updates) == 1
        update = cached["button"].updates[0]
        assert update["text"] == ("当前" if key == "1" else "使用")
        assert update["fg_color"] == ("primary" if key == "1" else "secondary")
        # CTk reconfigures its grid and font callbacks even for equal values.
        assert not {"font", "height", "corner_radius"}.intersection(update)
    assert all(not picker._row_cache[str(index)]["row"].updates for index in range(2, 300))
    assert all(not picker._row_cache[str(index)]["button"].updates for index in range(2, 300))


def test_selecting_current_node_keeps_action_without_repainting(picker):
    selected = []
    picker._on_select = selected.append
    picker._select("0")

    # Clicking "current" may still intentionally reapply the chosen node.
    assert selected == [picker._nodes[0]]
    assert not picker._row_cache["0"]["row"].updates
    assert not picker._row_cache["0"]["button"].updates


def test_reusing_unchanged_selected_row_does_not_repaint(picker):
    cached = picker._row_cache["0"]
    picker._reuse_row(cached["presentation"], relayout=False)
    assert not cached["row"].updates
    assert not cached["button"].updates
    assert not any(label.updates for label in cached["labels"])


def test_missing_row_cache_still_updates_visible_selection(picker):
    del picker._row_cache["0"]
    del picker._row_cache["1"]
    picker._select("1")
    assert picker._visible_node_rows["0"][1].updates[-1]["text"] == "使用"
    assert picker._visible_node_rows["1"][1].updates[-1]["text"] == "当前"


def test_hidden_selection_is_not_repainted(picker):
    del picker._visible_node_rows["1"]
    picker._select("1")
    assert picker._selected_key == "1"
    assert not picker._row_cache["1"]["row"].updates
    assert not picker._row_cache["1"]["button"].updates
