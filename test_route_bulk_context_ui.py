"""Filtered route editing carries its explicit context into batch drafts."""
import copy
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

import test_service_routes_dialog as fixtures
from ui.dialogs.service_route_bulk_dialog import DIRECT

editor = fixtures.editor


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Synthetic route editing must not access the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def _selected(dialog):
    return {key for key, value in dialog._vars.items() if value.get()}


def test_unfiltered_batch_keeps_empty_selection_and_drafts(editor, tk_root):
    _root, dialog, saved = editor
    before = copy.deepcopy(dialog._drafts)
    assert dialog._bulk_button.cget("text") == "批量设置"
    dialog._open_bulk_dialog()
    assert not _selected(dialog._bulk_dialog)
    assert dialog._drafts == before and not saved


def test_category_batch_preselects_only_visible_ai_targets(editor, tk_root):
    _root, dialog, saved = editor
    before = copy.deepcopy(dialog._drafts)
    dialog._set_category("AI 服务")
    assert dialog._bulk_button.cget("text") == "批量设置（3）"
    dialog._open_bulk_dialog()
    assert _selected(dialog._bulk_dialog) == {"openai", "claude", "google_ai"}
    assert dialog._drafts == before and not saved


def test_batch_flushes_pending_search_before_capturing_targets(editor, tk_root):
    _root, dialog, saved = editor
    dialog._set_category("AI 服务")
    dialog._search.set("Claude")
    assert dialog._filter_after_id is not None
    dialog._open_bulk_dialog()
    assert _selected(dialog._bulk_dialog) == {"claude"}
    assert dialog._bulk_button.cget("text") == "批量设置（1）"
    assert dialog._filter_after_id is None
    assert not saved


def test_empty_filtered_result_disables_batch_even_after_reenable(editor, tk_root):
    _root, dialog, saved = editor
    dialog._search.set("synthetic-no-such-route")
    dialog._filter_rows()
    assert dialog._bulk_button.cget("text") == "批量设置（0）"
    assert dialog._bulk_button.cget("state") == "disabled"
    dialog._set_editable(True)
    assert dialog._bulk_button.cget("state") == "disabled"
    dialog._open_bulk_dialog()
    assert dialog._bulk_dialog is None and not saved


@pytest.mark.parametrize("query,expected", [("Claude", {"claude"}), ("", set()), ("still-not-found", None)])
def test_real_batch_button_flushes_query_changed_after_empty_results(editor, tk_root, query, expected):
    _root, dialog, saved = editor
    dialog._search.set("no-matches")
    dialog._filter_rows()
    assert dialog._bulk_button.cget("state") == "disabled"
    dialog._search.set(query)
    assert dialog._bulk_button.cget("state") == "normal"
    assert dialog._bulk_button.cget("text") == "批量设置…"
    dialog._bulk_button.invoke()
    assert dialog._filter_after_id is None
    if expected is None:
        assert dialog._bulk_dialog is None
        assert dialog._bulk_button.cget("state") == "disabled"
    else:
        assert _selected(dialog._bulk_dialog) == expected
    assert not saved


def test_category_and_search_intersection_never_selects_hidden_rows(editor, tk_root):
    _root, dialog, saved = editor
    dialog._set_category("AI 服务")
    dialog._search.set("机房订阅 B")
    dialog._open_bulk_dialog()
    assert dialog._bulk_dialog is None
    assert dialog._visible_services == ()
    assert dialog._bulk_button.cget("state") == "disabled"
    assert not saved


def test_clear_filters_restores_explicit_empty_batch_selection(editor, tk_root):
    _root, dialog, saved = editor
    dialog._set_category("AI 服务")
    dialog._clear_filters()
    assert dialog._bulk_button.cget("text") == "批量设置"
    assert dialog._bulk_button.cget("state") == "normal"
    dialog._open_bulk_dialog()
    assert not _selected(dialog._bulk_dialog)
    assert not saved


def test_whitespace_is_not_an_active_search_for_batch(editor, tk_root):
    _root, dialog, saved = editor
    dialog._search.set("  ")
    dialog._open_bulk_dialog()
    assert not _selected(dialog._bulk_dialog)
    assert dialog._bulk_button.cget("text") == "批量设置"
    assert not saved


def test_scope_change_recomputes_filtered_target_count(editor, tk_root):
    _root, dialog, saved = editor
    first, second = dialog._scopes
    dialog._drafts[second]["service_profile_bindings"]["claude"] = "dc"
    dialog._drafts[second]["service_node_bindings"]["claude"] = "three"
    before = copy.deepcopy(dialog._drafts)
    dialog._search.set("家宽订阅 A")
    dialog._filter_rows()
    assert dialog._bulk_button.cget("text") == "批量设置（2）"
    dialog._switch_scope(second)
    assert dialog._bulk_button.cget("text") == "批量设置（1）"
    dialog._open_bulk_dialog()
    assert _selected(dialog._bulk_dialog) == {"openai"}
    dialog._accept_bulk_edit(second, ["openai"], DIRECT)
    assert dialog._drafts[first] == before[first]
    assert dialog._drafts[second]["service_profile_bindings"]["claude"] == "dc"
    assert dialog._drafts[second]["service_route_modes"]["openai"] == "direct"
    assert not saved


def test_busy_editor_cannot_reenable_batch_when_filter_changes(editor, tk_root):
    _root, dialog, saved = editor
    dialog._busy = True
    dialog._set_editable(False)
    dialog._set_category("AI 服务")
    assert dialog._bulk_button.cget("state") == "disabled"
    dialog._open_bulk_dialog()
    assert dialog._bulk_dialog is None
    dialog._busy = False
    dialog._set_editable(True)
    assert dialog._bulk_button.cget("state") == "normal"
    assert dialog._bulk_button.cget("text") == "批量设置（3）"
    assert not saved


@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_filtered_editor_and_batch_screenshots_preserve_drafts(editor, tk_root, scale):
    from tools.ui_visual_audit import capture_window_image
    root, dialog, saved = editor
    before = copy.deepcopy(dialog._drafts)
    previous = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        dialog._set_category("AI 服务")
        folder = Path(__file__).resolve().parent / "dist" / "proxy-simple-workflow-v2470"
        folder.mkdir(parents=True, exist_ok=True)
        until = time.monotonic() + 0.4
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        assert dialog._bulk_button.winfo_viewable()
        assert dialog._bulk_button.cget("text") == "批量设置（3）"
        capture_window_image(dialog, onscreen=True).save(folder / f"filtered-editor-{int(scale * 100)}.png")
        dialog._bulk_button.invoke()
        bulk = dialog._bulk_dialog
        until = time.monotonic() + 0.4
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        assert _selected(bulk) == {"openai", "claude", "google_ai"}
        assert "已带入筛选" in bulk._selection_note.cget("text")
        assert bulk._apply_button.winfo_rooty() + bulk._apply_button.winfo_height() <= bulk.winfo_rooty() + bulk.winfo_height()
        assert dialog._drafts == before and not saved
        capture_window_image(bulk, onscreen=True).save(folder / f"filtered-bulk-{int(scale * 100)}.png")
    finally:
        if dialog._bulk_dialog and dialog._bulk_dialog.winfo_exists():
            dialog._bulk_dialog.destroy()
        ctk.set_widget_scaling(previous)
        root.update()
