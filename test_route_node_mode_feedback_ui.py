"""Mode-specific node guidance must not change any routing selections."""

from types import SimpleNamespace

import pytest

from ui.dialogs.route_selection_dialogs import AUTO_MODE, FIXED_MODE, MODE_LABELS, POOL_MODE, RouteNodeDialog


@pytest.fixture
def mode_picker(tk_root):
    dialogs = []

    def create(*, selected_key="", selected_keys=None, with_pool=True, usable=True):
        singles, pools = [], []
        dialog = RouteNodeDialog(
            tk_root, service_label="合成 Claude Code", profile_name="合成订阅",
            nodes=[{"key": "one", "label": "合成节点一"}, {"key": "two", "label": "合成节点二"}],
            selected_key=selected_key, selected_keys=selected_keys,
            on_select=singles.append, on_select_pool=pools.append if with_pool else None,
            auto_route_usable=usable,
        )
        dialogs.append(dialog)
        tk_root.update()
        return SimpleNamespace(dialog=dialog, singles=singles, pools=pools, root=tk_root)

    yield create
    for dialog in reversed(dialogs):
        if dialog.winfo_exists():
            dialog.destroy()
        tk_root.update()


@pytest.mark.parametrize(
    "mode,options,required,unrelated",
    [
        (AUTO_MODE, {}, ("当前订阅", "随订阅刷新变化", "可能跨国家"), "只在勾选"),
        (POOL_MODE, {"selected_keys": ["two", "one"]},
         ("只在勾选", "按顺序", "不使用其他节点", "不保证出口国家"), "随订阅刷新变化"),
        (FIXED_MODE, {"selected_key": "one"},
         ("失效不自动切换", "不保证固定 IP / 国家"), "只在勾选"),
    ],
)
def test_initial_guidance_matches_only_current_mode(mode_picker, mode, options, required, unrelated):
    case = mode_picker(**options)
    assert case.dialog._mode == mode
    note = case.dialog._mode_note.cget("text")
    assert all(fragment in note for fragment in required)
    assert unrelated not in note
    assert "保存并应用后生效" in case.dialog._draft_note.cget("text")
    assert not case.singles and not case.pools


def test_changing_modes_updates_guidance_without_erasing_fixed_or_pool_choices(mode_picker):
    case = mode_picker(selected_key="one", selected_keys=["two", "one"])
    dialog = case.dialog
    notes = []
    for mode in (AUTO_MODE, FIXED_MODE, POOL_MODE):
        dialog._set_mode(mode)
        notes.append(dialog._mode_note.cget("text"))
        assert dialog._selected_key == "one"
        assert dialog._selected_keys == ["two", "one"]
        assert not case.singles and not case.pools
    assert len(set(notes)) == 3
    dialog._commit()
    assert case.pools == [["two", "one"]] and not case.singles


def test_node_click_replaces_automatic_guidance_with_fixed_guidance(mode_picker):
    case = mode_picker()
    dialog = case.dialog
    assert dialog._mode == AUTO_MODE
    dialog._list.selection_set(1)
    dialog._select_visible()
    assert dialog._mode == FIXED_MODE and dialog._selected_key == "two"
    assert "失效不自动切换" in dialog._mode_note.cget("text")
    assert "随订阅刷新变化" not in dialog._mode_note.cget("text")
    assert not case.singles and not case.pools


def test_no_pool_compatibility_rejects_hidden_mode_without_changing_guidance(mode_picker):
    case = mode_picker(selected_key="one", with_pool=False)
    dialog = case.dialog
    assert MODE_LABELS[POOL_MODE] not in dialog._modes.cget("values")
    note = dialog._mode_note.cget("text")
    dialog._set_mode(POOL_MODE)
    assert dialog._mode == FIXED_MODE and dialog._mode_note.cget("text") == note
    dialog._set_mode(AUTO_MODE)
    assert "可能跨国家" in dialog._mode_note.cget("text")
    dialog._commit()
    assert case.singles == [""] and not case.pools


@pytest.mark.parametrize("with_pool", [False, True])
def test_auto_unusable_remains_blocked_while_fixed_selection_stays_available(mode_picker, with_pool):
    case = mode_picker(selected_key="one", with_pool=with_pool, usable=False)
    dialog = case.dialog
    dialog._set_mode(AUTO_MODE)
    assert "可能跨国家" in dialog._mode_note.cget("text")
    assert "无法独立运行" in dialog._selection.cget("text")
    assert dialog._choose.cget("state") == "disabled"
    dialog._commit()
    assert not case.singles and not case.pools and dialog.winfo_exists()
    dialog._set_mode(FIXED_MODE)
    assert dialog._choose.cget("state") == "normal"
    assert "不保证固定 IP / 国家" in dialog._mode_note.cget("text")
    dialog._commit()
    assert case.singles == ["one"] and not case.pools
