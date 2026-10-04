"""Real, isolated editor controls keep common actions clear and safe."""
import copy
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

import test_service_routes_dialog as fixtures
from ui.dialogs import service_routes_dialog as ui

editor = fixtures.editor


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Editor UI tests must not access real networks")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def test_clean_editor_has_two_fixed_actions_and_save_does_not_reapply(editor, tk_root):
    root, dialog, saved = editor
    assert dialog._tools_row.master is dialog._table
    assert set(dialog._actions.grid_slaves()) == {dialog._close_button, dialog._save_button}
    assert dialog._save_button.cget("state") == "disabled"
    dialog._save_button.invoke()
    root.update()
    assert not saved
    dialog._set_editable(True)
    assert dialog._save_button.cget("state") == "disabled"
    assert dialog._reapply_button.cget("state") == "normal"


def test_undo_appears_only_for_current_draft_but_save_includes_other_scopes(editor, tk_root):
    _root, dialog, saved = editor
    dialog._select_node("claude", "日本 · 家宽 01")
    assert dialog._reset_button.winfo_manager() == "grid"
    assert dialog._save_button.cget("state") == "normal"
    assert dialog._reapply_button.cget("state") == "disabled"
    dialog._switch_scope(dialog._scopes[1])
    assert not dialog._reset_button.winfo_manager()
    assert dialog._save_button.cget("state") == "normal"
    dialog._reapply_current()
    assert not saved and "请先保存或撤销" in dialog._status.cget("text")
    dialog._switch_scope(dialog._scopes[0])
    dialog._reset_button.invoke()
    assert dialog._save_button.cget("state") == "disabled"
    assert not dialog._reset_button.winfo_manager()


def test_explicit_reapply_uses_only_current_scope_and_keeps_result_return_reachable(editor, tk_root):
    root, dialog, saved = editor
    before = copy.deepcopy(dialog._drafts)
    dialog._toggle_more()
    dialog._reapply_button.invoke()
    fixtures._wait(root, lambda: not dialog._busy)
    assert len(saved) == 1 and saved[0][0] == dialog._scope
    assert dialog._drafts == before
    assert dialog._content_view == "results"
    assert dialog._back_button.winfo_viewable()
    assert not dialog._table.winfo_viewable()
    dialog._back_button.invoke()
    root.update()
    assert dialog._content_view == "edit"
    assert not dialog._back_button.winfo_manager()
    assert dialog._save_button.cget("state") == "disabled"


def test_explicit_reapply_does_not_bypass_missing_remote_authority(editor, tk_root, monkeypatch):
    root, dialog, saved = editor
    prompts = []
    monkeypatch.setattr(ui, "ConfirmDialog", lambda *_args, **kw: prompts.append(kw))
    dialog._contexts[dialog._scope]["_authority_missing"] = True
    dialog._reapply_current()
    assert prompts and "确认重建" in prompts[0]["title"]
    assert not saved
    prompts[0]["on_confirm"]()
    fixtures._wait(root, lambda: not dialog._busy)
    assert len(saved) == 1


def test_explicit_reapply_does_not_bypass_privacy_conflict(editor, tk_root):
    _root, dialog, saved = editor
    scope = dialog._scope
    dialog._select_profile("youtube", ui.DIRECT_PROFILE)
    dialog._originals[scope] = copy.deepcopy(dialog._drafts[scope])
    dialog._contexts[scope]["strict_privacy"] = True
    dialog._changed()
    dialog._reapply_current()
    assert "直连与严格隐私冲突" in dialog._status.cget("text")
    assert dialog._preview_open and dialog._back_button.winfo_manager() == "grid"
    assert not saved


@pytest.mark.parametrize("view", ["edit", "preview", "results"])
@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_slim_footer_scrolling_tools_and_back_action_at_scale(editor, tk_root, view, scale):
    from tools.ui_visual_audit import capture_window_image
    root, dialog, saved = editor
    previous = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        # Let CTk's one-second scaling size lock expire before sizing the
        # actual window used by the screenshot/layout assertions.
        until = time.monotonic() + 1.1
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        dialog.geometry("900x680")
        dialog._set_category("AI 服务")
        if view == "preview":
            dialog._select_node("claude", "日本 · 家宽 01")
            dialog._preview_toggle.invoke()
        elif view == "results":
            dialog._reapply_current()
            fixtures._wait(root, lambda: not dialog._busy)
        until = time.monotonic() + 0.35
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        assert dialog._content_view == view
        footer = dialog._actions.master
        assert footer.winfo_height() < dialog.winfo_height() * 0.4
        assert dialog._save_button.winfo_rooty() + dialog._save_button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        if view == "edit":
            assert dialog._tools_row.master is dialog._table
            dialog._reveal_table_widget(dialog._tools_row)
        else:
            assert dialog._back_button.winfo_viewable()
        folder = Path(__file__).resolve().parent / "dist" / "proxy-simple-workflow-v2471" / "editor"
        folder.mkdir(parents=True, exist_ok=True)
        capture_window_image(dialog, onscreen=True).save(folder / f"{view}-{int(scale * 100)}.png")
        if view != "edit":
            dialog._back_button.invoke()
            root.update()
            assert dialog._table.winfo_viewable()
        assert len(saved) == (1 if view == "results" else 0)
    finally:
        ctk.set_widget_scaling(previous)
        root.update()


def test_custom_input_scrolls_into_view_without_consuming_fixed_footer(editor, tk_root):
    root, dialog, saved = editor
    dialog._toggle_custom_form()
    root.update()
    assert dialog._custom_form.master is dialog._table
    assert dialog._custom_form.winfo_viewable()
    dialog._custom_entry.insert(0, "synthetic.example.invalid")
    dialog._add_button.invoke()
    assert any(row["label"] == "synthetic.example.invalid" for row in dialog._rows.values())
    assert not saved
