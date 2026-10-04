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
        assert update["text"] == ("已选" if key == "1" else "选择")
        assert update["fg_color"] == ("primary" if key == "1" else "secondary")
        # CTk reconfigures its grid and font callbacks even for equal values.
        assert not {"font", "height", "corner_radius"}.intersection(update)
    assert all(not picker._row_cache[str(index)]["row"].updates for index in range(2, 300))
    assert all(not picker._row_cache[str(index)]["button"].updates for index in range(2, 300))


def test_selecting_current_node_keeps_action_without_repainting(picker):
    selected = []
    picker._on_select = selected.append
    picker._select("0")

    # Clicking "selected" may still intentionally refill the pending node.
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
    assert picker._visible_node_rows["0"][1].updates[-1]["text"] == "选择"
    assert picker._visible_node_rows["1"][1].updates[-1]["text"] == "已选"


def test_hidden_selection_is_not_repainted(picker):
    del picker._visible_node_rows["1"]
    picker._select("1")
    assert picker._selected_key == "1"
    assert not picker._row_cache["1"]["row"].updates
    assert not picker._row_cache["1"]["button"].updates


def test_empty_result_summary_omits_unused_counters_but_identifies_test_records(picker):
    picker._summary_label = Control()
    picker._summary_counts = {}
    picker._render_plan_pending = False
    picker._last_match_count = len(picker._nodes)
    picker._update_summary_label()
    assert picker._summary_label.updates[-1]["text"] == "共 300 个；测速记录 0/300；可连 0"


def test_summary_keeps_cancelled_incomplete_expired_and_loading_feedback(picker):
    picker._summary_label = Control()
    picker._summary_counts = {"ok": 10, "measured": 20, "quality": 8, "high_quality": 3,
                              "cached_quality": 2, "cancelled": 1, "incomplete": 2, "expired": 4}
    picker._render_plan_pending = True
    picker._checkbox_sync_after_id = "synthetic"
    picker._checked_keys = {"0", "1"}
    picker._update_summary_label(match_count=11)
    text = picker._summary_label.updates[-1]["text"]
    for part in ("测速记录 20/300", "可连 10", "质量记录 8", "高质 3", "质量缓存 2",
                 "勾选 2", "匹配 11", "已取消 1", "未完成 2", "已过期 4",
                 "正在加载节点列表", "正在更新勾选显示"):
        assert part in text
    assert "已测延迟" not in text  # A record can be cancelled/incomplete, not a completed latency.


def test_group_count_is_checkbox_scope_not_running_node(picker):
    assert "勾选 4" in picker._group_header_text("合成地区", 10, 3, 1, 4)
    assert "已选" not in picker._group_header_text("合成地区", 10, 3, 1, 4)


def test_missing_quality_result_points_to_existing_action(picker):
    picker._quality_combo = SimpleNamespace(get=lambda: "家宽高质")
    assert picker._empty_message(total=3, quality_count=0) == "暂无质量结果，请先点击“IP 质量检测”"
