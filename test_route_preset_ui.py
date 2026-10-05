"""Native Windows preset interactions with synthetic data and draft callbacks."""
import copy
from pathlib import Path
import time

import customtkinter as ctk
import pytest

from core.route_presets import AI_NODE_STRATEGIES, ROUTE_PRESETS
from test_route_presets import catalog, preferences
from test_service_routes_dialog import _wait
from ui.dialogs.service_routes_dialog import ServiceRoutesDialog


@pytest.fixture
def editor(tk_root):
    saved = []
    # Metadata changes after loading must be visible to the preset without
    # implicitly changing the editor's routing draft.
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


def review(child, root):
    child._preview_tab.invoke()
    if not child._show_changes:
        child._change_details_button.invoke()
    root.update()
    assert child._view == "preview"
    assert child._preview.winfo_viewable() and not child._setup.winfo_viewable()


def capture(child, name):
    from tools.ui_visual_audit import capture_window_image
    output = Path("dist/route-preset-ui")
    output.mkdir(parents=True, exist_ok=True)
    capture_window_image(child).save(output / name)


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


def test_optional_adjustments_review_and_back_keep_choices(editor, tk_root):
    parent, child, saved = open_preset(editor, tk_root)
    original = copy.deepcopy(parent._drafts)
    child._replace.set(True)
    child._scheme.set(ROUTE_PRESETS["datacenter"]["label"])
    child._refresh()
    widgets = {key: pair for key, pair in child._preview_rows.items()}
    for _ in range(3):
        child._accept_button.invoke()
        tk_root.update()
        assert child._view == "preview" and child.winfo_exists()
        assert parent._drafts == original and not saved
        child._setup_tab.invoke()
        tk_root.update()
        assert child._view == "setup" and child._replace.get()
        assert child._scheme.get() == ROUTE_PRESETS["datacenter"]["label"]
        assert child._preview_rows == widgets
    child._back_button.invoke()
    assert child._view == "preview" and child.winfo_exists()
    child._back_button.invoke()
    assert not child.winfo_exists() and parent._drafts == original


@pytest.mark.parametrize("refresh", [True, False])
def test_editing_choices_after_review_needs_fresh_review(editor, tk_root, refresh):
    parent, child, saved = open_preset(editor, tk_root)
    original = copy.deepcopy(parent._drafts)
    child._setup_tab.invoke()
    child._accept()  # Calling acceptance while adjusting is still guarded.
    assert parent._drafts == original and child.winfo_exists()
    review(child, tk_root)
    child._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
    if refresh:
        child._refresh()
    child._accept()
    assert child._view == "setup" and not saved and parent._drafts == original
    review(child, tk_root)
    assert child._plan["draft"]["service_route_modes"]["youtube"] == "direct"
    child._accept_button.invoke()
    assert parent._drafts[parent._scope]["service_route_modes"]["youtube"] == "direct" and not saved


def test_programmatic_choice_change_is_refreshed_when_entering_review(editor, tk_root):
    _, child, saved = open_preset(editor, tk_root)
    child._scheme.set(ROUTE_PRESETS["datacenter"]["label"])
    review(child, tk_root)
    assert set(child._plan["draft"]["service_profile_bindings"].values()) == {"dc"}
    assert not saved


def test_unchanged_preview_refresh_avoids_repainting_and_repacking_sources(editor, tk_root, monkeypatch):
    _, child, _ = open_preset(editor, tk_root)
    tk_root.update()
    calls = []
    widgets = [child._status, child._accept_button, child._back_button, child._setup_tab, child._preview_tab,
               child._description, child._ai_hint, *child._source_combos.values(),
               *(row[1] for row in child._source_rows.values()),
               *(widget for pair in child._preview_rows.values() for widget in pair)]
    for widget in widgets:
        original = widget.configure
        def configure(*args, _original=original, **kwargs):
            calls.append(kwargs)
            return _original(*args, **kwargs)
        monkeypatch.setattr(widget, "configure", configure)
    packs = []
    for card, _ in child._source_rows.values():
        original_pack = card.pack
        def pack(*args, _original=original_pack, **kwargs):
            packs.append(kwargs)
            return _original(*args, **kwargs)
        monkeypatch.setattr(card, "pack", pack)
    for _ in range(3):
        child._refresh()
    assert calls == [] and packs == []


def test_replanning_shows_old_fixed_pool_and_new_strategy(editor, tk_root):
    parent, _ = editor
    parent._drafts[parent._scope]["service_profile_bindings"]["claude"] = "dc"
    parent._drafts[parent._scope]["service_node_pools"]["claude"] = ["d2", "d1"]
    _, child, saved = open_preset(editor, tk_root)
    child._replace.set(True)
    child._refresh()
    review(child, tk_root)
    text = child._preview_rows["claude"][1].cget("text")
    assert "原：" in text and "新：" in text and "自选 2 个候选" in text and "合成家宽" in text
    capture(child, "preset-before-after.png")
    assert not saved


def test_kept_invalid_binding_is_visible_as_needing_attention(editor, tk_root):
    parent, _ = editor
    parent._drafts[parent._scope]["service_profile_bindings"]["claude"] = "deleted"
    parent._drafts[parent._scope]["service_node_bindings"]["claude"] = "gone"
    _, child, saved = open_preset(editor, tk_root)
    review(child, tk_root)
    assert "保留 · 需检查" in child._preview_rows["claude"][0].cget("text")
    assert "订阅已失效" in child._preview_rows["claude"][1].cget("text")
    assert "1 项待处理" in child._status.cget("text")
    assert child._plan["draft"]["service_node_bindings"]["claude"] == "gone" and not saved
    capture(child, "preset-kept-invalid.png")


def test_subscription_guidance_handoff_restores_grab_without_accepting_draft(editor, tk_root, monkeypatch):
    parent, saved = editor
    for item in parent._catalog:
        item["network_type"] = "unknown"
    opened = []
    monkeypatch.setattr(parent, "_open_subscription_tags", lambda: opened.append(parent.grab_current()))
    before = copy.deepcopy(parent._drafts)
    _, child, _ = open_preset(editor, tk_root)
    child._body._parent_canvas.yview_moveto(1)
    tk_root.update()
    assert child._manage_sources_button.winfo_viewable()
    assert "标记" in child._source_help.cget("text")
    capture(child, "preset-missing-guidance.png")
    child._manage_sources_button.invoke()
    child._manage_sources()  # Repeated callbacks cannot reopen a second modal.
    assert not child.winfo_exists() and opened == [parent]
    assert parent._drafts == before and not saved


def test_single_source_presets_hide_unused_controls_and_restore_order(editor, tk_root):
    _, child, _ = open_preset(editor, tk_root)
    expected = [pair[0] for pair in child._source_rows.values()]
    for preset, wanted in (("ai_only", expected[:1]), ("datacenter", expected[1:]),
                           ("balanced", expected), ("datacenter", expected[1:]), ("balanced", expected)):
        child._scheme.set(ROUTE_PRESETS[preset]["label"])
        child._refresh()
        assert child._sources_box.pack_slaves() == wanted


def test_long_names_and_errors_stay_scrollable_without_hiding_actions(editor, tk_root):
    parent, _ = editor
    for item in parent._catalog:
        item["name"] = "合成长订阅名称 · " * 24
    _, child, saved = open_preset(editor, tk_root)
    child.geometry("540x480")
    tk_root.update()
    review(child, tk_root)
    child._on_accept = lambda **_kwargs: (_ for _ in ()).throw(ValueError("合成错误详情；" * 120))
    child._accept_button.invoke()
    tk_root.update()
    assert child._error_detail.winfo_viewable()
    assert child._body._parent_canvas.winfo_height() >= 100 * child._body._get_widget_scaling()
    assert child._accept_button.winfo_rooty() + child._accept_button.winfo_height() <= child.winfo_rooty() + child.winfo_height()
    assert child._accept_button.cget("state") == "disabled" and not saved
    capture(child, "preset-long-error.png")


def test_accept_changes_only_current_draft_and_explicit_save_applies(editor, tk_root):
    parent, child, saved = open_preset(editor, tk_root)
    second = parent._scopes[1]
    original = copy.deepcopy(parent._drafts)
    review(child, tk_root)
    child._accept_button.invoke()
    tk_root.update()
    assert not child.winfo_exists() and parent.grab_current() is parent
    assert not saved and parent._originals == original
    assert parent._drafts[second] == original[second]
    assert len(parent._drafts[parent._scope]["service_profile_bindings"]) == 11
    assert not parent._preview_open and parent._content_view == "edit"
    assert parent._table.winfo_manager() and "预设草稿" in parent._status.cget("text")
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
    review(child, tk_root)
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
    child._setup_tab.invoke()
    tk_root.update()
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


@pytest.mark.parametrize("race", ["scope", "draft", "catalog", "primary", "busy", "privacy"])
def test_stale_preview_never_overwrites_new_context(editor, tk_root, race):
    parent, child, saved = open_preset(editor, tk_root)
    if race == "scope":
        parent._scope = parent._scopes[1]
    elif race == "draft":
        parent._drafts[parent._scope]["service_route_modes"]["youtube"] = "direct"
    elif race == "catalog":
        parent._catalog[0]["nodes"] = []
    elif race == "primary":
        parent._catalog[0]["selected_node_key"] = "h2"
    elif race == "busy":
        parent._busy = True
    else:
        child._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
        child._refresh()
        parent._contexts[parent._scope]["strict_privacy"] = True
    original = copy.deepcopy(parent._drafts)
    try:
        review(child, tk_root)
        child._accept_button.invoke()
        assert child.winfo_exists() and not saved and parent._drafts == original
        assert any(word in child._error_detail.cget("text") for word in ("变化", "严格隐私"))
        assert child._accept_button.cget("state") == "disabled"
    finally:
        parent._busy = False


def test_privacy_conflict_and_empty_catalog_disable_accept(editor, tk_root):
    parent, _ = editor
    parent._contexts[parent._scope]["strict_privacy"] = True
    _, child, saved = open_preset(editor, tk_root)
    child._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
    child._refresh()
    assert child._plan is None and child._accept_button.cget("state") == "disabled"
    assert "严格隐私" in child._error_detail.cget("text")
    child._accept()
    assert not saved
    child._scheme.set(ROUTE_PRESETS["balanced"]["label"])
    child._refresh()
    assert child._accept_button.cget("state") == "normal"
    child.destroy()
    parent._catalog = []
    parent._open_preset_dialog()
    empty = parent._preset_dialog
    assert "还没有订阅" in empty._source_help.cget("text")
    review(empty, tk_root)
    assert empty._accept_button.cget("state") == "disabled"
    assert empty._accept_button.cget("text") == "先处理订阅问题"
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
        child._setup_tab.invoke()
        tk_root.update()
        viewport = child._body._parent_canvas
        assert viewport.winfo_height() >= 100 * child._body._get_widget_scaling()
        for widget in (child._scheme, child._accept_button, child._replace_checkbox):
            assert widget.winfo_rootx() >= child.winfo_rootx()
            assert widget.winfo_rootx() + widget.winfo_width() <= child.winfo_rootx() + child.winfo_width()
        assert child._accept_button.winfo_rooty() + child._accept_button.winfo_height() <= child.winfo_rooty() + child.winfo_height()
        output = Path("dist/route-preset-ui")
        output.mkdir(parents=True, exist_ok=True)
        name = "small-scaled" if scale > 1 else "wide"
        capture_window_image(child).save(output / f"preset-{name}-top.png")
        review(child, tk_root)
        capture_window_image(child).save(output / f"preset-{name}-review.png")
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


def test_preset_ui_defaults_to_fixed_ai_without_touching_saved_routes(editor, tk_root):
    parent, child, saved = open_preset(editor, tk_root)
    assert child._choices()["ai_strategy"] == "fixed"
    assert child._plan["draft"]["service_node_bindings"] == dict.fromkeys(("openai", "claude", "google_ai"), "h1")
    assert "不保证" in child._ai_hint.cget("text")
    assert not parent._drafts[parent._scope]["service_node_bindings"] and not saved
    review(child, tk_root)
    assert "固定节点" in child._preview_rows["claude"][1].cget("text")
    assert not child._auto_ack_checkbox.winfo_manager()


def test_automatic_ai_requires_explicit_review_acknowledgement(editor, tk_root):
    parent, child, saved = open_preset(editor, tk_root)
    original = copy.deepcopy(parent._drafts)
    child._ai_strategy.set(AI_NODE_STRATEGIES["auto"])
    child._refresh()
    review(child, tk_root)
    assert child._auto_ack_checkbox.winfo_viewable()
    assert child._accept_button.cget("state") == "disabled"
    child._accept()  # A direct callback cannot bypass acknowledgement.
    assert parent._drafts == original and not saved
    capture(child, "preset-ai-auto-warning.png")
    child._auto_ack_checkbox._canvas.event_generate("<Button-1>", x=8, y=8)
    tk_root.update()
    assert child._accept_button.cget("state") == "normal"
    child._accept_button.invoke()
    assert not parent._drafts[parent._scope]["service_node_bindings"] and not saved


@pytest.mark.parametrize("change", ["scheme", "source", "strategy"])
def test_auto_ack_is_invalidated_when_preset_choices_change(editor, tk_root, change):
    parent, child, _ = open_preset(editor, tk_root)
    child._ai_strategy.set(AI_NODE_STRATEGIES["auto"])
    child._refresh()
    review(child, tk_root)
    child._auto_ack_checkbox.toggle()
    child._setup_tab.invoke()
    if change == "scheme":
        child._scheme.set(ROUTE_PRESETS["datacenter"]["label"])
    elif change == "source":
        choice = next(label for label, key in child._options["residential"].items() if key)
        child._source_combos["residential"].set(choice)
    else:
        child._ai_strategy.set(AI_NODE_STRATEGIES["fixed"])
    child._refresh()
    assert not child._auto_ack.get() and child._reviewed_choices is None
    assert not parent._drafts[parent._scope]["service_profile_bindings"]


def test_preset_kept_ai_choices_do_not_require_auto_ack_for_website_changes(editor, tk_root):
    parent, _ = editor
    parent._drafts[parent._scope]["service_profile_bindings"] = dict.fromkeys(("openai", "claude", "google_ai"), "home")
    _, child, _ = open_preset(editor, tk_root)
    assert not child._plan["draft"]["service_node_bindings"]
    child._ai_strategy.set(AI_NODE_STRATEGIES["auto"])
    child._refresh()
    review(child, tk_root)
    assert child._accept_button.cget("state") == "normal" and not child._auto_ack_checkbox.winfo_manager()


def test_fixed_preset_missing_primary_is_not_disguised_as_available(editor, tk_root):
    parent, _ = editor
    parent._catalog[0]["selected_node_key"] = "vanished"
    _, child, saved = open_preset(editor, tk_root)
    review(child, tk_root)
    assert "3 项待处理" in child._status.cget("text")
    assert "首选节点已失效" in child._preview_rows["claude"][1].cget("text")
    assert not child._plan["draft"]["service_node_bindings"] and not saved


def test_saved_automatic_ai_is_explicitly_preserved_not_claimed_fixed(tk_root):
    saved = []
    existing = preferences()
    existing["service_profile_bindings"] = dict.fromkeys(("openai", "claude", "google_ai"), "home")
    parent = ServiceRoutesDialog(
        tk_root, scopes=["合成已有标记"], load_preferences=lambda _: copy.deepcopy(existing), catalog_loader=catalog,
        apply_preferences=lambda *args: saved.append(args))
    try:
        _wait(tk_root, lambda: not parent._busy and parent.winfo_viewable())
        before = copy.deepcopy(parent._drafts)
        assert before == parent._originals and not parent._changes
        assert before[parent._scope]["service_profile_bindings"]["claude"] == "home"
        assert not before[parent._scope]["service_node_bindings"]
        parent._open_preset_dialog()
        child = parent._preset_dialog
        _wait(tk_root, child.winfo_viewable)
        assert "仍保留 3 项已有 AI 自动切换线路" in child._ai_hint.cget("text")
        assert not child._plan["draft"]["service_node_bindings"]
        child._replace_checkbox.toggle()
        tk_root.update()
        assert "仍保留" not in child._ai_hint.cget("text")
        assert child._plan["draft"]["service_node_bindings"] == dict.fromkeys(("openai", "claude", "google_ai"), "h1")
        assert parent._drafts == before and not saved
    finally:
        parent.destroy()
        tk_root.update()
