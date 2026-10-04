"""Native isolated checks for the compact node-and-backup workflow."""

from pathlib import Path
import time
from types import SimpleNamespace

import customtkinter as ctk
import pytest

from ui.dialogs.route_selection_dialogs import AUTO_MODE, FIXED_MODE, MODE_LABELS, POOL_MODE, RouteNodeDialog


@pytest.fixture
def simple_picker(tk_root):
    dialogs = []

    def create(*, selected_key="node-1", selected_keys=None):
        singles, pools = [], []
        dialog = RouteNodeDialog(
            tk_root, service_label="合成 Claude Code", profile_name="合成家宽订阅 A",
            nodes=[{"key": f"node-{index}", "label": f"合成日本节点 {index:02d}"} for index in range(8)],
            selected_key=selected_key, selected_keys=selected_keys,
            on_select=singles.append, on_select_pool=pools.append,
        )
        dialogs.append(dialog)
        tk_root.update()
        return SimpleNamespace(dialog=dialog, singles=singles, pools=pools, root=tk_root)

    yield create
    for dialog in reversed(dialogs):
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


def test_short_mode_choices_are_ordered_from_narrow_to_wide(simple_picker, tk_root):
    case = simple_picker()
    assert case.dialog._modes.cget("values") == [MODE_LABELS[mode] for mode in (FIXED_MODE, POOL_MODE, AUTO_MODE)]
    assert case.dialog._choose.cget("text") == "使用固定节点"
    assert "保存并应用后生效" in case.dialog._draft_note.cget("text")
    assert not case.singles and not case.pools


def test_add_backup_keeps_primary_and_only_commits_after_explicit_use(simple_picker, tk_root):
    case = simple_picker()
    dialog = case.dialog
    dialog._select_mode_label(MODE_LABELS[POOL_MODE])
    assert dialog._selected_keys == ["node-1"]
    assert dialog._list.curselection() == (1,)
    assert "暂无备用" in dialog._selection.cget("text")
    dialog._list.selection_set(4)
    dialog._select_visible()
    assert dialog._choose.cget("text") == "使用 2 个候选"
    assert not case.singles and not case.pools
    dialog._choose.invoke()
    assert case.pools == [["node-1", "node-4"]] and not case.singles


def test_first_choice_shortcut_preserves_pool_and_updates_boundary_actions(simple_picker, tk_root):
    case = simple_picker(selected_keys=["node-1", "node-4", "node-7"])
    dialog = case.dialog
    assert all(button.cget("state") == "disabled" for button in dialog._pool_actions.values())
    dialog._pool_list.selection_set(2)
    dialog._update_pool_actions()
    assert dialog._pool_actions["first"].cget("state") == "normal"
    assert dialog._pool_actions["down"].cget("state") == "disabled"
    dialog._pool_actions["first"].invoke()
    assert dialog._selected_keys == ["node-7", "node-1", "node-4"]
    assert dialog._pool_actions["first"].cget("state") == "disabled"
    assert dialog._pool_actions["up"].cget("state") == "disabled"
    assert dialog._pool_actions["down"].cget("state") == "normal"
    assert not case.singles and not case.pools


def test_missing_fixed_choice_is_not_replaced_when_adding_backups(simple_picker, tk_root):
    case = simple_picker(selected_key="missing-primary")
    dialog = case.dialog
    dialog._set_mode(POOL_MODE)
    assert dialog._selected_keys == ["missing-primary"]
    assert "已失效" in dialog._pool_list.get(0)
    assert dialog._choose.cget("state") == "disabled"
    dialog._commit()
    assert not case.singles and not case.pools and dialog.winfo_exists()


def test_switching_back_to_automatic_keeps_expansion_warning_and_original_choices(simple_picker, tk_root):
    case = simple_picker(selected_keys=["node-4", "node-1"])
    dialog = case.dialog
    dialog._select_mode_label(MODE_LABELS[AUTO_MODE])
    assert dialog._choose.cget("text") == "使用订阅自动"
    assert "可能跨国家" in dialog._selection.cget("text")
    assert dialog._selected_keys == ["node-4", "node-1"]
    assert dialog._selected_key == "node-1"
    dialog.destroy()
    assert not case.singles and not case.pools


@pytest.mark.parametrize("scaling", [1.0, 2.5])
def test_compact_picker_footer_and_modes_remain_visible_at_dpi(simple_picker, tk_root, scaling):
    from tools.ui_visual_audit import capture_window_image

    previous = ctk.ScalingTracker.widget_scaling
    case = simple_picker(selected_keys=["node-1", "node-4", "node-7"])
    dialog = case.dialog
    try:
        ctk.set_widget_scaling(scaling)
        dialog.geometry("560x650")
        dialog.lift()
        for _ in range(12):
            tk_root.update()
            time.sleep(0.03)
        canvas = dialog._body._parent_canvas
        assert dialog._modes.winfo_width() <= canvas.winfo_width()
        for widget in (dialog._choose, dialog._draft_note):
            assert widget.winfo_rooty() + widget.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        destination = Path("dist/proxy-simple-workflow-v2470/nodes")
        destination.mkdir(parents=True, exist_ok=True)
        capture_window_image(dialog, onscreen=True).save(destination / f"node-pool-{int(scaling * 100)}.png")
        assert not case.singles and not case.pools
    finally:
        ctk.set_widget_scaling(previous)
        tk_root.update()
