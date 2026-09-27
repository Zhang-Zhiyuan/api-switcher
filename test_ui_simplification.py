"""Native UI flows with synthetic routes only; no live proxy/account changes."""
import copy
import threading
import time

import customtkinter as ctk
import pytest

from test_service_routes_dialog import _catalog, _preferences, _wait
from ui.dialogs.service_routes_dialog import DIRECT_PROFILE, ServiceRoutesDialog
from ui.widgets.action_group import wrap_action_group
from ui.widgets.service_route_overview import ServiceRouteOverview


def settle(root, seconds=0.15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        root.update()
        time.sleep(0.01)


@pytest.fixture
def editor(tk_root):
    applied = []
    dialog = ServiceRoutesDialog(tk_root, scopes=["合成本机", "合成 SSH"],
        load_preferences=lambda _: _preferences(), catalog_loader=_catalog,
        apply_preferences=lambda *args: applied.append(args) or "仅合成应用回调")
    _wait(tk_root, lambda: not dialog._busy and dialog.winfo_viewable())
    yield dialog, applied
    dialog.destroy()
    tk_root.update()


def test_review_is_a_read_only_view_and_return_preserves_filter_and_row_widgets(editor, tk_root):
    dialog, applied = editor
    dialog._set_category("网站")
    dialog._search.set("YouTube")
    dialog._select_profile("youtube", DIRECT_PROFILE)
    before = copy.deepcopy(dialog._drafts)
    rows = {key: row["tile"] for key, row in dialog._rows.items()}
    for _ in range(3):
        dialog._preview_toggle.invoke()
        tk_root.update()
        assert dialog._preview.winfo_viewable() and not dialog._table.winfo_viewable()
        assert "YouTube" in dialog._preview.get("1.0", "end")
        dialog._preview_toggle.invoke()
        tk_root.update()
        assert dialog._table.winfo_viewable() and not dialog._preview.winfo_viewable()
        assert dialog._category == "网站" and dialog._search.get() == "YouTube"
    assert dialog._drafts == before and not applied
    assert rows == {key: row["tile"] for key, row in dialog._rows.items()}


def test_more_settings_are_discoverable_and_reload_retains_draft(editor, tk_root):
    dialog, applied = editor
    assert not dialog._more_tools.winfo_viewable()
    dialog._select_profile("youtube", DIRECT_PROFILE)
    before = copy.deepcopy(dialog._drafts)
    dialog._more_toggle.invoke()
    tk_root.update()
    for widget in (dialog._tags_button, dialog._tag_routes_button, dialog._reload_button, dialog._legacy_cleanup_button):
        assert widget.winfo_viewable()
    dialog._reload_button.invoke()
    _wait(tk_root, lambda: not dialog._busy)
    assert dialog._drafts == before and not applied
    dialog._preview_toggle.invoke()
    tk_root.update()
    assert not dialog._more_open and not dialog._more_tools.winfo_viewable()
    assert dialog._preview.winfo_viewable()


def test_apply_results_use_content_area_and_allow_retry_without_losing_failed_draft(editor, tk_root):
    dialog, applied = editor
    dialog._select_profile("youtube", DIRECT_PROFILE)
    before = copy.deepcopy(dialog._drafts)
    original = dialog._applier
    def fail(*args):
        raise ValueError("合成连接失败")
    dialog._applier = fail
    dialog._save_button.invoke()
    _wait(tk_root, lambda: not dialog._busy)
    assert dialog._details.winfo_viewable() and not dialog._table.winfo_viewable()
    assert "合成连接失败" in dialog._details.get("1.0", "end")
    assert dialog._drafts == before and not applied
    assert dialog._preview_toggle.cget("text") == "返回编辑"
    dialog._preview_toggle.invoke()
    tk_root.update()
    assert dialog._table.winfo_viewable()
    assert dialog._rows["youtube"]["profile"].get() == DIRECT_PROFILE
    dialog._applier = original
    dialog._save_button.invoke()
    _wait(tk_root, lambda: not dialog._busy)
    assert dialog._details.winfo_viewable()
    assert len(applied) == 1 and not dialog._changes
    dialog._preview_toggle.invoke()
    tk_root.update()
    assert dialog._table.winfo_viewable() and dialog._reset_button.cget("state") == "disabled"


def test_new_target_from_review_returns_to_editor_and_keeps_unsubmitted_input(editor, tk_root):
    dialog, applied = editor
    dialog._custom_toggle.invoke()
    dialog._custom_entry.insert(0, "https://example.invalid/path")
    dialog._preview_toggle.invoke()
    assert not dialog._custom_open
    dialog._custom_toggle.invoke()
    tk_root.update()
    assert dialog._table.winfo_viewable() and dialog._custom_form.winfo_viewable()
    assert dialog._custom_entry.get() == "https://example.invalid/path"
    dialog._add_button.invoke()
    assert dialog._search.get() == "example.invalid" and not applied
    assert dialog._category == "自定义"


def test_review_button_cannot_access_unloaded_preferences(tk_root):
    release = threading.Event()
    def load(_scope):
        release.wait(5)
        return _preferences()
    dialog = ServiceRoutesDialog(tk_root, scopes=["合成"], load_preferences=load,
                                 catalog_loader=_catalog, apply_preferences=lambda *_: pytest.fail("unexpected apply"))
    try:
        assert dialog._preview_toggle.cget("state") == "disabled"
        dialog._toggle_preview()
        assert not dialog._preview_open and not dialog._drafts
        release.set()
        _wait(tk_root, lambda: not dialog._busy)
        assert dialog._preview_toggle.cget("state") == "normal"
    finally:
        release.set()
        dialog.destroy()
        tk_root.update()


@pytest.mark.parametrize("geometry,scale", [("720x680", 1), ("860x720", 1.25), ("680x520", 1.5)])
def test_disclosure_controls_and_save_remain_visible_in_small_windows(editor, tk_root, geometry, scale):
    dialog, applied = editor
    try:
        ctk.set_widget_scaling(scale)
        settle(tk_root, 1.1)
        dialog.geometry(geometry)
        dialog._custom_toggle.invoke()
        dialog._custom_entry.insert(0, "unsaved.example.invalid")
        dialog._more_toggle.invoke()
        settle(tk_root)
        assert not dialog._custom_open and dialog._more_open
        for widget in (dialog._more_toggle, dialog._save_button, dialog._tags_button, dialog._reload_button):
            assert widget.winfo_viewable()
            assert widget.winfo_rootx() >= dialog.winfo_rootx()
            assert widget.winfo_rootx() + widget.winfo_width() <= dialog.winfo_rootx() + dialog.winfo_width()
            assert widget.winfo_rooty() + widget.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        dialog._custom_toggle.invoke()
        settle(tk_root)
        assert not dialog._more_open and dialog._custom_open
        assert dialog._custom_entry.get() == "unsaved.example.invalid" and not applied
    finally:
        ctk.set_widget_scaling(1)


@pytest.mark.parametrize("scale", [1, 1.5])
@pytest.mark.parametrize("wide_columns", [3, 4])
def test_action_groups_wrap_without_losing_callbacks_states_or_recreating_controls(tk_root, scale, wide_columns):
    window = ctk.CTkToplevel(tk_root)
    group = ctk.CTkFrame(window)
    group.pack(fill="x", padx=10, pady=10)
    calls = []
    ctk.CTkLabel(group, text="检测").pack()
    buttons = []
    for i in range(5):
        button = ctk.CTkButton(group, text=f"合成操作 {i}", width=112, command=lambda i=i: calls.append(i))
        button.pack()
        buttons.append(button)
    hint = ctk.CTkLabel(group, text="仅合成布局；此处不代表实际连通。" * 4, width=120)
    hint.pack()
    buttons[1].configure(state="disabled")
    wrap_action_group(group, hint=hint, wide_columns=wide_columns)
    try:
        _wait(tk_root, lambda: window.winfo_viewable())
        ctk.set_widget_scaling(scale)
        settle(tk_root, 1.1)
        children = group.winfo_children()
        for width in (760, 360, 580, 760):
            window.geometry(f"{width}x650")
            settle(tk_root)
            available = group.winfo_width() / group._get_widget_scaling()
            columns = wide_columns if available >= 520 else 2 if available >= 300 else 1
            assert len({button.grid_info()["column"] for button in buttons}) == columns
            for button in buttons:
                assert button.winfo_viewable()
                assert button.winfo_rootx() >= group.winfo_rootx()
                assert button.winfo_rootx() + button.winfo_width() <= group.winfo_rootx() + group.winfo_width()
                assert button._text_label.winfo_reqwidth() <= button.winfo_width()
            assert hint.winfo_height() >= hint._label.winfo_reqheight()
            assert group.winfo_children() == children
        for button in buttons:
            button.invoke()
        assert calls == [0, 2, 3, 4]
        assert buttons[1].cget("state") == "disabled"
    finally:
        ctk.set_widget_scaling(1)
        window.destroy()
        tk_root.update()


@pytest.mark.parametrize("width,columns", [(760, 2), (480, 2), (260, 1)])
def test_overview_primary_and_diagnostic_actions_fit_and_remain_reachable(tk_root, width, columns):
    window = ctk.CTkToplevel(tk_root)
    window.geometry(f"{width}x620")
    calls = []
    overview = ServiceRouteOverview(window, command=lambda service: calls.append(("edit", service)),
                                    inspect_command=lambda: calls.append(("inspect", "")))
    overview.pack(fill="x", padx=10)
    try:
        _wait(tk_root, lambda: window.winfo_viewable())
        settle(tk_root)
        assert overview._header_action_columns == columns
        for button in (overview._manage, overview._inspect):
            assert button.winfo_viewable()
            assert button.winfo_rootx() >= window.winfo_rootx()
            assert button.winfo_rootx() + button.winfo_width() <= window.winfo_rootx() + window.winfo_width()
            button.invoke()
        assert calls == [("edit", ""), ("inspect", "")]
        overview.set_enabled(False)
        overview._manage.invoke()
        overview._inspect.invoke()
        assert len(calls) == 2
    finally:
        window.destroy()
        tk_root.update()
