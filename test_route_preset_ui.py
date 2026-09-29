"""Native Windows preset interactions with synthetic data and draft callbacks."""
import copy
from pathlib import Path
import time

import customtkinter as ctk
import pytest

from core.route_presets import ROUTE_PRESETS
from test_route_presets import catalog, preferences
from test_service_routes_dialog import _wait
from ui.dialogs.service_routes_dialog import ServiceRoutesDialog


@pytest.fixture
def editor(tk_root):
    saved = []
    # Initial unknown labels avoid the older auto-seeding path; the preset sees
    # labels after loading. This also tests opening after catalog metadata changes.
    unlabelled = catalog()
    for item in unlabelled:
        item["network_type"] = "unknown"
    dialog = ServiceRoutesDialog(
        tk_root, scopes=["合成 Win11", "合成 SSH"], load_preferences=lambda _: preferences(),
        catalog_loader=lambda: unlabelled,
        apply_preferences=lambda *args: saved.append(copy.deepcopy(args)) or "合成应用成功",
    )
    _wait(tk_root, lambda: not dialog._busy and dialog.winfo_viewable())
    dialog._catalog = catalog()
    yield dialog, saved
    if dialog.winfo_exists():
        dialog.destroy()
    tk_root.update()


def open_preset(editor, tk_root):
    dialog, saved = editor
    dialog._preset_button.invoke()
    child = dialog._preset_dialog
    _wait(tk_root, child.winfo_viewable)
    return dialog, child, saved


def test_preview_and_cancel_have_no_side_effects_and_restore_editor_grab(editor, tk_root):
    parent, child, saved = open_preset(editor, tk_root)
    original = copy.deepcopy(parent._drafts)
    assert child._plan["changed_services"]
    assert parent._drafts == original and not saved
    parent._open_preset_dialog()
    assert parent._preset_dialog is child
    child.destroy()
    tk_root.update()
    assert parent.grab_current() is parent
    assert parent._drafts == original and not saved


def test_accept_changes_only_current_draft_and_explicit_save_applies(editor, tk_root):
    parent, child, saved = open_preset(editor, tk_root)
    second = parent._scopes[1]
    original = copy.deepcopy(parent._drafts)
    child._accept_button.invoke()
    tk_root.update()
    assert not child.winfo_exists() and parent.grab_current() is parent
    assert not saved and parent._originals == original
    assert parent._drafts[second] == original[second]
    assert len(parent._drafts[parent._scope]["service_profile_bindings"]) == 11
    assert parent._preview_open and "预设草稿" in parent._status.cget("text")
    assert parent._manually_edited[parent._scope] == set(child._plan["changed_services"])
    parent._save_button.invoke()
    _wait(tk_root, lambda: not parent._busy)
    assert len(saved) == 1 and saved[0][0] != second


def test_ssh_preset_keeps_windows_draft_and_reuses_normal_apply_path(editor, tk_root):
    parent, saved = editor
    windows, ssh = parent._scopes
    before = copy.deepcopy(parent._drafts[windows])
    parent._switch_scope(ssh)
    _, child, _ = open_preset(editor, tk_root)
    child._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
    child._replace.set(True)
    child._refresh()
    child._accept_button.invoke()
    assert parent._drafts[windows] == before and not saved
    assert parent._drafts[ssh]["service_route_modes"]["youtube"] == "direct"
    parent._save_button.invoke()
    _wait(tk_root, lambda: not parent._busy)
    assert len(saved) == 1 and saved[0][0] == ssh


def test_preset_entry_cannot_open_during_apply_or_after_close(editor, tk_root):
    parent, saved = editor
    parent._busy = True
    parent._set_editable(False)
    assert parent._preset_button.cget("state") == "disabled"
    parent._open_preset_dialog()
    assert parent._preset_dialog is None
    parent._busy = False
    parent._set_editable(True)
    assert parent._preset_button.cget("state") == "normal"
    parent.destroy()
    parent._open_preset_dialog()
    assert parent._preset_dialog is None and not saved


def test_existing_choices_require_explicit_replanning_and_preview_updates(editor, tk_root):
    parent, _ = editor
    parent._drafts[parent._scope]["service_profile_bindings"]["claude"] = "dc"
    parent._drafts[parent._scope]["service_node_pools"]["claude"] = ["d2", "d1"]
    parent, child, saved = open_preset(editor, tk_root)
    assert child._plan["draft"]["service_node_pools"]["claude"] == ["d2", "d1"]
    assert "保留" in child._preview_rows["claude"][0].cget("text")
    child._replace_checkbox._canvas.event_generate("<Button-1>", x=8, y=8)
    tk_root.update()
    assert child._replace.get()
    assert child._plan["draft"]["service_profile_bindings"]["claude"] == "home"
    assert "claude" not in child._plan["draft"]["service_node_pools"]
    assert "将修改" in child._preview_rows["claude"][0].cget("text")
    assert parent._drafts[parent._scope]["service_node_pools"]["claude"] == ["d2", "d1"]
    assert not saved


def test_preset_changes_enable_only_needed_selectors_and_allow_manual_source(editor, tk_root):
    parent, _ = editor
    extra = copy.deepcopy(parent._catalog[0])
    extra["id"] = "home-other"
    parent._catalog.append(extra)  # Duplicate display names must remain selectable.
    _, child, saved = open_preset(editor, tk_root)
    options = child._options["residential"]
    assert len(options) == 3 and len(set(options.values())) == 3
    label = next(label for label, key in options.items() if key == "home-other")
    child._source_combos["residential"].set(label)
    child._refresh()
    assert child._plan["sources"]["residential"]["id"] == "home-other"
    child._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
    child._refresh()
    assert child._source_combos["datacenter"].cget("state") == "disabled"
    assert child._plan["draft"]["service_route_modes"]["youtube"] == "direct"
    child._scheme.set(ROUTE_PRESETS["datacenter"]["label"])
    child._refresh()
    assert child._source_combos["residential"].cget("state") == "disabled"
    assert child._source_combos["datacenter"].cget("state") == "readonly"
    assert set(child._plan["draft"]["service_profile_bindings"].values()) == {"dc"}
    assert not saved


@pytest.mark.parametrize("race", ["scope", "draft", "catalog", "busy", "privacy"])
def test_stale_preview_never_overwrites_new_context(editor, tk_root, race):
    parent, child, saved = open_preset(editor, tk_root)
    if race == "scope":
        parent._scope = parent._scopes[1]
    elif race == "draft":
        parent._drafts[parent._scope]["service_route_modes"]["youtube"] = "direct"
    elif race == "catalog":
        parent._catalog[0]["nodes"] = []
    elif race == "busy":
        parent._busy = True
    else:
        child._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
        child._refresh()
        parent._contexts[parent._scope]["strict_privacy"] = True
    original = copy.deepcopy(parent._drafts)
    try:
        child._accept_button.invoke()
        assert child.winfo_exists() and not saved and parent._drafts == original
        assert any(word in child._status.cget("text") for word in ("变化", "严格隐私"))
    finally:
        parent._busy = False


def test_privacy_conflict_and_empty_catalog_disable_accept(editor, tk_root):
    parent, _ = editor
    parent._contexts[parent._scope]["strict_privacy"] = True
    _, child, saved = open_preset(editor, tk_root)
    child._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
    child._refresh()
    assert child._plan is None and child._accept_button.cget("state") == "disabled"
    assert "严格隐私" in child._status.cget("text")
    child._accept()
    assert not saved
    child._scheme.set(ROUTE_PRESETS["balanced"]["label"])
    child._refresh()
    assert child._accept_button.cget("state") == "normal"
    child.destroy()
    parent._catalog = []
    parent._open_preset_dialog()
    empty = parent._preset_dialog
    assert empty._accept_button.cget("state") == "disabled"
    assert "11 项待处理" in empty._status.cget("text")
    assert not saved


@pytest.mark.parametrize("geometry,scale", [("800x740", 1), ("540x480", 1.5)])
def test_preset_real_scroll_and_small_high_dpi_footer_reachable(editor, tk_root, geometry, scale):
    from tools.ui_visual_audit import capture_window_image

    parent, child, saved = open_preset(editor, tk_root)
    original_scale = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        deadline = time.monotonic() + 1.1
        while time.monotonic() < deadline:
            tk_root.update()
            time.sleep(0.01)
        child.geometry(geometry)
        tk_root.update()
        viewport = child._body._parent_canvas
        assert viewport.winfo_height() >= 100 * child._get_widget_scaling()
        for widget in (child._scheme, child._accept_button, child._replace_checkbox):
            assert widget.winfo_rootx() >= child.winfo_rootx()
            assert widget.winfo_rootx() + widget.winfo_width() <= child.winfo_rootx() + child.winfo_width()
        assert child._accept_button.winfo_rooty() + child._accept_button.winfo_height() <= child.winfo_rooty() + child.winfo_height()
        output = Path("dist/route-preset-ui")
        output.mkdir(parents=True, exist_ok=True)
        name = "small-scaled" if scale > 1 else "wide"
        capture_window_image(child).save(output / f"preset-{name}-top.png")
        viewport.yview_moveto(1)
        tk_root.update()
        last = child._preview_rows["telegram"][1]
        assert last.winfo_rooty() >= viewport.winfo_rooty()
        assert last.winfo_rooty() + last.winfo_height() <= viewport.winfo_rooty() + viewport.winfo_height()
        capture_window_image(child).save(output / f"preset-{name}-bottom.png")
        assert not saved
        child._accept_button.invoke()
        assert parent._drafts[parent._scope]["service_profile_bindings"]["youtube"] == "dc"
        assert not saved
    finally:
        ctk.set_widget_scaling(original_scale)
        tk_root.update()
