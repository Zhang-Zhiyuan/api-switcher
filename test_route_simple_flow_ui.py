"""Compact route editing: same saved intent, fewer irrelevant controls."""
import copy
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

import test_service_routes_dialog as fixtures
from ui.dialogs.service_routes_dialog import DEFAULT_PROFILE, DIRECT_PROFILE

editor = fixtures.editor
_wait = fixtures._wait


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Synthetic route editing must not access the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def test_direct_and_default_hide_node_controls_without_changing_other_routes(editor):
    root, dialog, saved = editor
    before = copy.deepcopy(dialog._drafts)
    row = dialog._rows["youtube"]
    row["profile"].cget("command")(DIRECT_PROFILE)
    root.update()
    assert row["node"].winfo_manager() == row["node_caption"].winfo_manager() == ""
    assert row["detail"].winfo_manager() == ""
    assert dialog._drafts[dialog._scope]["service_route_modes"]["youtube"] == "direct"
    assert dialog._drafts[dialog._scopes[1]] == before[dialog._scopes[1]]
    assert not saved
    row["profile"].cget("command")(DEFAULT_PROFILE)
    assert not row["node"].winfo_manager()
    row["profile"].cget("command")("机房订阅 B")
    assert row["node"].winfo_manager() == "grid"
    assert dialog._drafts[dialog._scope]["builtin_sites"]["youtube"]
    assert not saved


def test_more_details_never_hide_warnings_or_mutate_fixed_and_pool_choices(editor):
    root, dialog, saved = editor
    scope = dialog._scope
    dialog._drafts[scope]["service_node_bindings"].pop("openai")
    dialog._drafts[scope]["service_node_pools"]["openai"] = ["two", "one"]
    dialog._changed()
    before = copy.deepcopy(dialog._drafts)
    row = dialog._rows["openai"]
    assert not row["detail"].winfo_manager()
    assert "2" in row["node"].get()
    for _ in range(2):
        dialog._more_toggle.invoke()
        root.update()
        assert bool(row["detail"].winfo_manager()) == dialog._more_open
        assert dialog._drafts == before and not saved
    dialog._select_profile("claude", "机房订阅 B")
    warning = dialog._rows["claude"]
    assert warning["description"]["warning"]
    assert warning["detail"].winfo_manager() == "grid"
    assert "无符合 AI 自动筛选" in warning["detail"].cget("text")


def test_reselecting_saved_subscription_enables_site_without_resetting_pin(editor):
    root, dialog, saved = editor
    dialog._toggle("youtube", False)
    assert not dialog._drafts[dialog._scope]["builtin_sites"]["youtube"]
    row = dialog._rows["youtube"]
    row["profile"].cget("command")(row["saved_choice"])
    root.update()
    assert row["enabled"].get()
    assert dialog._drafts[dialog._scope]["service_node_bindings"]["youtube"] == "three"
    assert not saved


def test_bulk_returns_to_editing_and_still_requires_explicit_save(editor):
    root, dialog, saved = editor
    from ui.dialogs.service_route_bulk_dialog import DIRECT
    dialog._accept_bulk_edit(dialog._scope, ["youtube"], DIRECT)
    root.update()
    assert dialog._content_view == "edit" and not dialog._preview_open
    assert not saved and dialog._originals[dialog._scope]["service_node_bindings"]["youtube"] == "three"
    dialog._save_button.invoke()
    _wait(root, lambda: not dialog._busy)
    assert len(saved) == 1 and saved[0][1]["service_route_modes"]["youtube"] == "direct"


@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_compact_editor_layout_and_visible_actions_at_multiple_scales(editor, tk_root, scale):
    root, dialog, saved = editor
    from tools.ui_visual_audit import capture_window_image
    previous = ctk.ScalingTracker.widget_scaling
    before = copy.deepcopy(dialog._drafts)
    try:
        ctk.set_widget_scaling(scale)
        dialog.geometry("1040x760")
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert dialog._save_button.winfo_viewable()
        assert dialog._save_button.winfo_rooty() + dialog._save_button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        viewport = dialog._table._parent_canvas
        assert viewport.winfo_viewable() and viewport.winfo_height() >= 140
        packed = dialog._table.pack_slaves()
        assert packed[:2] == [dialog._header, dialog._filters]
        folder = Path(__file__).resolve().parent / "dist" / "proxy-simple-workflow-v2469" / "editor"
        folder.mkdir(parents=True, exist_ok=True)
        capture_window_image(dialog).save(folder / f"top-{int(scale * 100)}.png")
        # The first route must be reachable and fully visible, not merely
        # managed by Tk somewhere outside a collapsed scrolling viewport.
        row = dialog._rows["openai"]["tile"]
        bounds = viewport.bbox("all")
        top = row.winfo_rooty() - viewport.winfo_rooty() + viewport.canvasy(0)
        viewport.yview_moveto(max(0, top - bounds[1]) / (bounds[3] - bounds[1]))
        root.update()
        assert row.winfo_rooty() >= viewport.winfo_rooty() - 2
        assert row.winfo_rooty() + row.winfo_height() <= viewport.winfo_rooty() + viewport.winfo_height() + 2
        for row in dialog._rows.values():
            for key in ("target", "profile", "node", "detail"):
                widget = row[key]
                if widget.winfo_manager():
                    assert widget.winfo_rootx() >= viewport.winfo_rootx() - 2
                    assert widget.winfo_rootx() + widget.winfo_width() <= viewport.winfo_rootx() + viewport.winfo_width() + 2
        assert dialog._drafts == before and not saved
        capture_window_image(dialog).save(folder / f"editor-{int(scale * 100)}.png")
        dialog._more_toggle.invoke()
        root.update()
        assert dialog._table.pack_slaves()[:4] == [dialog._header, dialog._filters, dialog._tools_row, dialog._more_tools]
        assert dialog._more_tools.winfo_rooty() >= viewport.winfo_rooty() - 2
        assert dialog._more_tools.winfo_rooty() + dialog._more_tools.winfo_height() <= viewport.winfo_rooty() + viewport.winfo_height() + 2
        assert dialog._save_button.winfo_rooty() + dialog._save_button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        assert dialog._drafts == before and not saved
        capture_window_image(dialog).save(folder / f"more-{int(scale * 100)}.png")
    finally:
        ctk.set_widget_scaling(previous)
        root.update()
        assert dialog._table.pack_slaves()[:2] == [dialog._header, dialog._filters]


def test_more_tools_can_return_from_preview_without_losing_draft(editor):
    root, dialog, saved = editor
    dialog._select_profile("youtube", DIRECT_PROFILE)
    before = copy.deepcopy(dialog._drafts)
    dialog._preview_toggle.invoke()
    assert dialog._content_view == "preview"
    dialog._more_toggle.invoke()
    root.update()
    assert dialog._content_view == "edit" and dialog._more_open
    assert dialog._more_tools.winfo_manager()
    assert dialog._drafts == before and not saved


@pytest.mark.parametrize("view", ["preview", "results"])
def test_scaling_while_reviewing_keeps_hidden_editor_pack_anchors(editor, monkeypatch, view):
    root, dialog, saved = editor
    dialog._select_profile("youtube", DIRECT_PROFILE)
    if view == "preview":
        dialog._preview_toggle.invoke()
    else:
        dialog._save_button.invoke()
        _wait(root, lambda: not dialog._busy)
        assert len(saved) == 1
    assert dialog._content_view == view
    before = copy.deepcopy(dialog._drafts)
    errors = []
    monkeypatch.setattr(root, "report_callback_exception", lambda *error: errors.append(error))
    previous = ctk.ScalingTracker.widget_scaling
    try:
        for scale in (2.5, 1.0):
            ctk.set_widget_scaling(scale)
            root.update()
            assert not dialog._table.winfo_viewable()
            assert dialog._save_button.winfo_viewable()
        dialog._preview_toggle.invoke()
        root.update()
        assert dialog._content_view == "edit"
        assert dialog._table.pack_slaves()[:2] == [dialog._header, dialog._filters]
        assert dialog._drafts == before and not errors
    finally:
        ctk.set_widget_scaling(previous)
        root.update()


def test_wheel_over_header_and_filters_scrolls_the_same_route_surface(editor):
    root, dialog, saved = editor
    root.update()
    viewport = dialog._table._parent_canvas
    before = copy.deepcopy(dialog._drafts)
    for widget in (dialog._intro_notice, dialog._scope_combo._entry, dialog._count_label):
        viewport.yview_moveto(0)
        root.update()
        widget.event_generate("<MouseWheel>", delta=-120)
        root.update()
        assert viewport.yview()[0] > 0, f"wheel over {widget} did not reach the route scroller"
    assert dialog._drafts == before and not saved
