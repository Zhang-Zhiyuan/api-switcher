"""One visible route choice for bulk edits; no persistence or live network."""
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

import test_route_bulk_ui as fixtures
from core import proxy_routing
from ui.dialogs import service_route_bulk_dialog as ui

bulk = fixtures.bulk


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Bulk UI tests cannot use the network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def test_one_dropdown_requires_both_explicit_route_and_targets(bulk):
    dialog = bulk.dialog
    assert dialog._profile.get() == ui.CHOOSE_ROUTE
    assert len([child for child in dialog._profile.master.winfo_children()
                if isinstance(child, ctk.CTkComboBox)]) == 1
    assert not dialog._node_button.winfo_manager()
    dialog._select_group("sites")
    assert dialog._apply_button.cget("state") == "disabled"
    dialog._profile.cget("command")(ui.DIRECT)
    assert dialog._apply_button.cget("state") == "normal"
    assert not bulk.applied
    dialog._select_group("none")
    assert dialog._apply_button.cget("state") == "disabled"


@pytest.mark.parametrize("choice", [ui.FOLLOW, ui.DIRECT, ui.DISABLE, ui.ENABLE, ui.TAGGED])
def test_special_choices_commit_the_same_underlying_operation(bulk, choice):
    dialog = bulk.dialog
    dialog._vars["youtube"].set(True)
    dialog._profile.cget("command")(choice)
    assert dialog._profile.get() == choice
    assert dialog._operation.get() == choice
    assert not dialog._node_button.winfo_manager()
    dialog._apply_button.invoke()
    assert bulk.applied == [((["youtube"], choice), {"profile_id": "", "node_key": "", "node_keys": []})]


def test_changing_displayed_route_preserves_scratch_pool_until_source_changes(bulk):
    dialog = bulk.dialog
    dialog._select_route("合成家宽")
    dialog._choose_pool(["two", "one"], profile_id="home")
    dialog._select_route(ui.DIRECT)
    assert not dialog._node_button.winfo_manager()
    dialog._select_route("合成家宽")
    assert dialog._operation.get() == ui.SET_ROUTE
    assert dialog._node_keys == ["two", "one"]
    assert dialog._node_button.winfo_manager()
    dialog._select_route("合成非家宽")
    assert not dialog._node_keys and not dialog._node_key
    assert not bulk.applied


def test_filtered_initial_selection_is_explicit_and_intersected_with_current_targets(tk_root):
    calls = []
    dialog = ui.RouteBulkDialog(tk_root, rows=proxy_routing.route_rows({}), catalog=fixtures._catalog(),
                                initial_services=["youtube", "youtube", "missing"], scope_label="合成 Windows",
                                on_apply=lambda *a, **kw: calls.append((a, kw)))
    try:
        assert [key for key, var in dialog._vars.items() if var.get()] == ["youtube"]
        assert "已带入主列表筛选" in dialog._selection_note.cget("text")
        assert dialog._apply_button.cget("state") == "disabled"
        assert not calls
        dialog._select_route(ui.DIRECT)
        dialog._apply_button.invoke()
        assert calls[0][0] == (["youtube"], ui.DIRECT)
    finally:
        if dialog.winfo_exists():
            dialog.destroy()


@pytest.mark.parametrize("name", [ui.CHOOSE_ROUTE, *ui.OPERATIONS])
def test_subscription_names_cannot_shadow_route_actions(tk_root, name):
    catalog = [{**fixtures._catalog()[0], "name": name}]
    calls = []
    dialog = ui.RouteBulkDialog(tk_root, rows=proxy_routing.route_rows({}), catalog=catalog,
                                on_apply=lambda *a, **kw: calls.append((a, kw)))
    try:
        label = next(label for label, key in dialog._profiles.items() if key == "home")
        assert label != name
        dialog._select_route(label)
        assert dialog._operation.get() == ui.SET_ROUTE and dialog._profile_id == "home"
        assert not calls
    finally:
        dialog.destroy()


@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_compact_bulk_route_choice_screenshots(bulk, tk_root, scale):
    from tools.ui_visual_audit import capture_window_image
    dialog, root = bulk.dialog, bulk.root
    previous = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        dialog.geometry("760x700")
        dialog._select_group("sites")
        dialog._select_route(ui.DIRECT)
        until = time.monotonic() + 0.4
        while time.monotonic() < until:
            root.update()
            time.sleep(0.01)
        assert dialog._profile.winfo_viewable()
        assert not dialog._node_button.winfo_manager()
        assert dialog._apply_button.winfo_rooty() + dialog._apply_button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        viewport = dialog._body._parent_canvas
        assert viewport.winfo_height() > 100
        folder = Path(__file__).resolve().parent / "dist" / "proxy-simple-workflow-v2470"
        folder.mkdir(parents=True, exist_ok=True)
        capture_window_image(dialog, onscreen=True).save(folder / f"bulk-{int(scale * 100)}.png")
        assert not bulk.applied
    finally:
        ctk.set_widget_scaling(previous)
        root.update()
