"""Native draft-only UI checks for explicit ordered failover candidates."""

from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from ui.dialogs.route_selection_dialogs import AUTO_MODE, FIXED_MODE, MAX_POOL_NODES, POOL_MODE, RouteNodeDialog


def _nodes(count=8):
    return [{"key": f"node-{index}", "label": f"{'日本' if index % 2 else '美国'} · 合成节点 {index:03d}"}
            for index in range(count)]


@pytest.fixture
def pool_picker(tk_root):
    dialogs = []

    def create(*, nodes=None, selected_keys=None, selected_key=""):
        singles, pools = [], []
        dialog = RouteNodeDialog(
            tk_root, service_label="YouTube", profile_name="合成非家宽订阅",
            nodes=_nodes() if nodes is None else nodes, selected_key=selected_key,
            selected_keys=selected_keys, on_select=singles.append, on_select_pool=pools.append,
        )
        dialogs.append(dialog)
        tk_root.update()
        return SimpleNamespace(dialog=dialog, singles=singles, pools=pools, root=tk_root)

    yield create
    for dialog in reversed(dialogs):
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


def _choose_visible(dialog, *indices):
    dialog._list.selection_clear(0, "end")
    for index in indices:
        dialog._list.selection_set(index)
    dialog._select_visible()


def test_pool_selection_only_commits_to_draft_callback_on_explicit_save(pool_picker):
    case = pool_picker()
    dialog = case.dialog
    assert POOL_MODE in dialog._modes.cget("values")
    dialog._set_mode(POOL_MODE)
    _choose_visible(dialog, 1, 4)
    case.root.update()
    assert dialog._selected_keys == ["node-1", "node-4"]
    assert "2/16" in dialog._pool_caption.cget("text")
    assert not case.pools and not case.singles
    dialog._commit()
    assert case.pools == [["node-1", "node-4"]]
    assert not case.singles


def test_search_keeps_hidden_selections_and_only_toggles_visible_nodes(pool_picker):
    case = pool_picker(selected_keys=["node-4", "node-1"])
    dialog = case.dialog
    dialog._search.insert(0, "日本")
    dialog._filter()
    assert [item["key"] for item in dialog._visible] == ["node-1", "node-3", "node-5", "node-7"]
    _choose_visible(dialog, 0, 1)
    assert dialog._selected_keys == ["node-4", "node-1", "node-3"]
    _choose_visible(dialog, 1)
    assert dialog._selected_keys == ["node-4", "node-3"]
    dialog._search.delete(0, "end")
    dialog._filter()
    case.root.update()
    assert {dialog._visible[index]["key"] for index in dialog._list.curselection()} == {"node-4", "node-3"}
    assert not case.pools


def test_pool_reordering_and_removal_preserve_explicit_priority(pool_picker):
    case = pool_picker(selected_keys=["node-4", "node-1", "node-7"])
    dialog = case.dialog
    dialog._pool_list.selection_set(2)
    dialog._move_pool(-1)
    assert dialog._selected_keys == ["node-4", "node-7", "node-1"]
    dialog._move_pool(-1)
    assert dialog._selected_keys == ["node-7", "node-4", "node-1"]
    dialog._move_pool(-1)
    assert dialog._selected_keys == ["node-7", "node-4", "node-1"]
    dialog._remove_pool()
    assert dialog._selected_keys == ["node-4", "node-1"]
    dialog._commit()
    assert case.pools == [["node-4", "node-1"]]


def test_missing_candidate_is_preserved_visible_and_blocks_save_until_removed(pool_picker):
    selected = ["node-1", "deleted-stable-key", "node-4"]
    case = pool_picker(selected_keys=selected)
    dialog = case.dialog
    assert dialog._selected_keys == selected
    assert "已失效" in dialog._pool_list.get(1)
    assert dialog._choose.cget("state") == "disabled"
    dialog._commit()
    assert not case.pools and dialog.winfo_exists()
    dialog._pool_list.selection_set(1)
    dialog._remove_pool()
    assert selected == ["node-1", "deleted-stable-key", "node-4"]
    assert dialog._choose.cget("state") == "normal"
    dialog._commit()
    assert case.pools == [["node-1", "node-4"]]


def test_selection_limit_rejects_only_excess_new_candidates(pool_picker):
    case = pool_picker(nodes=_nodes(20))
    dialog = case.dialog
    dialog._set_mode(POOL_MODE)
    _choose_visible(dialog, *range(18))
    assert dialog._selected_keys == [f"node-{index}" for index in range(MAX_POOL_NODES)]
    assert "最多选择" in dialog._selection.cget("text")
    assert len(dialog._list.curselection()) == MAX_POOL_NODES


def test_loaded_oversize_pool_is_not_silently_truncated(pool_picker):
    original = [f"node-{index}" for index in range(17)]
    case = pool_picker(nodes=_nodes(20), selected_keys=original)
    dialog = case.dialog
    assert dialog._selected_keys == original
    assert dialog._choose.cget("state") == "disabled"
    dialog._commit()
    assert not case.pools
    dialog._pool_list.selection_set(16)
    dialog._remove_pool()
    assert len(dialog._selected_keys) == MAX_POOL_NODES
    assert len(original) == 17
    assert dialog._choose.cget("state") == "normal"


def test_cancel_drops_pool_edits_and_never_invokes_callbacks(pool_picker):
    case = pool_picker(selected_keys=["node-1"])
    _choose_visible(case.dialog, 1, 2)
    case.dialog.destroy()
    assert not case.pools and not case.singles


@pytest.mark.parametrize("mode,key", [(AUTO_MODE, ""), (FIXED_MODE, "node-2")])
def test_legacy_modes_still_use_single_key_callback(pool_picker, mode, key):
    case = pool_picker(selected_keys=["node-1", "node-4"], selected_key="node-2")
    case.dialog._set_mode(mode)
    case.dialog._commit()
    assert case.singles == [key]
    assert not case.pools


def test_return_or_double_click_does_not_prematurely_commit_pool(pool_picker):
    case = pool_picker(selected_keys=["node-1", "node-4"])
    assert case.dialog._commit_from_list() == "break"
    assert case.dialog.winfo_exists()
    assert not case.pools and not case.singles


def test_single_pool_candidate_explains_lack_of_backup(pool_picker):
    case = pool_picker(selected_keys=["node-1"])
    assert "暂无备用" in case.dialog._selection.cget("text")
    assert case.dialog._choose.cget("state") == "normal"


def test_selecting_deep_in_long_list_preserves_scroll_position(pool_picker):
    case = pool_picker(nodes=_nodes(500))
    case.dialog._set_mode(POOL_MODE)
    case.root.update()
    case.dialog._list.yview_moveto(0.75)
    before = case.dialog._list.yview()[0]
    _choose_visible(case.dialog, 375)
    assert abs(case.dialog._list.yview()[0] - before) < 0.02


def test_pool_toggle_keeps_keyboard_position_without_rebuilding_large_list(pool_picker, monkeypatch):
    case = pool_picker(nodes=_nodes(2000))
    dialog = case.dialog
    dialog._set_mode(POOL_MODE)
    dialog._list.activate(1500)
    dialog._list.selection_anchor(1490)
    dialog._list.yview_moveto(0.75)
    before = dialog._list.yview()[0]

    def unexpected_rebuild():
        pytest.fail("a candidate click must not filter and rebuild the entire subscription")

    monkeypatch.setattr(dialog, "_filter", unexpected_rebuild)
    _choose_visible(dialog, 1500)
    assert dialog._list.index("active") == 1500
    assert dialog._list.index("anchor") == 1490
    assert abs(dialog._list.yview()[0] - before) < 0.02
    assert dialog._list.get(1500).startswith("☑")
    assert dialog._list.get(1499).startswith("☐")
    _choose_visible(dialog, 1500, 1501)
    assert dialog._selected_keys == ["node-1500", "node-1501"]
    dialog._pool_list.selection_set(0)
    dialog._remove_pool()
    assert dialog._selected_keys == ["node-1501"]
    assert dialog._list.get(1500).startswith("☐")
    assert dialog._list.curselection() == (1501,)
    assert dialog._list.index("active") == 1500


def test_incremental_pool_checks_preserve_other_selections_when_toggled_in_reverse_order(pool_picker):
    case = pool_picker(selected_keys=["node-1", "node-4", "node-7"])
    dialog = case.dialog
    _choose_visible(dialog, 0, 2, 4, 6)
    assert dialog._selected_keys == ["node-4", "node-0", "node-2", "node-6"]
    assert dialog._list.curselection() == (0, 2, 4, 6)
    assert [index for index in range(8) if dialog._list.get(index).startswith("☑")] == [0, 2, 4, 6]


def capture_preview(directory):
    """Create isolated wide/narrow screenshots without real subscriptions."""
    import customtkinter as ctk
    from tools.ui_visual_audit import capture_window_image

    destination = Path(directory)
    destination.mkdir(parents=True, exist_ok=True)
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.title("隔离候选池预览（合成数据）")
    root.geometry("320x180+30+30")
    root.update()
    dialog = RouteNodeDialog(
        root, service_label="YouTube", profile_name="合成非家宽订阅 B",
        nodes=_nodes(30), selected_key="", selected_keys=["node-1", "node-4", "node-9"],
        on_select=lambda _key: None, on_select_pool=lambda _keys: None,
    )
    try:
        for name, geometry in (("wide", "700x710"), ("narrow", "500x650")):
            dialog.geometry(geometry)
            dialog.lift()
            for _ in range(15):
                root.update()
                time.sleep(0.03)
            capture_window_image(dialog).save(destination / f"node-pool-{name}.png")
    finally:
        dialog.destroy()
        root.destroy()


if __name__ == "__main__":
    import sys
    capture_preview(sys.argv[1])
