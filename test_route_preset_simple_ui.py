"""Compact preset presentation preserves explicit review and route safety."""
import copy
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

from core.route_presets import AI_NODE_STRATEGIES, ROUTE_PRESETS, plan_route_preset
from test_route_presets import catalog, preferences
from ui.dialogs.route_preset_dialog import RoutePresetDialog


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("preset presentation must not connect to a network")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture
def preset(tk_root):
    views = []

    def create(existing=None, sources=None):
        prefs = preferences() if existing is None else existing
        subscriptions = catalog() if sources is None else sources
        accepted = []
        view = RoutePresetDialog(tk_root, scope="合成设备", preferences=prefs, catalog=subscriptions,
                                 on_accept=lambda **choice: accepted.append(choice))
        views.append(view)
        tk_root.update()
        return view, accepted

    yield create
    for view in views:
        if view.winfo_exists():
            view.destroy()
    tk_root.update()


def test_default_shows_results_with_adjustments_optional(preset, tk_root):
    before, sources = preferences(), catalog()
    original = copy.deepcopy((before, sources))
    view, accepted = preset(before, sources)
    assert not view._advanced.winfo_manager()
    assert view._view == "preview" and view._reviewed_choices == view._choices()
    assert not view._replace_checkbox.winfo_viewable()
    assert not any(combo.winfo_viewable() for combo in view._source_combos.values())
    assert "固定到上述节点" in view._preview_policy.cget("text")
    assert "已有手动选择保持不变" in view._preview_policy.cget("text")
    assert "合成家宽 A" in view._summary_rows["ai"][2].cget("text")
    assert "合成非家宽 B" in view._summary_rows["sites"][2].cget("text")
    assert not any(tile.winfo_manager() for tile in view._preview_tiles.values())
    view._setup_tab.invoke()
    initial = copy.deepcopy(view._plan)
    for _ in range(2):
        view._advanced_button.invoke()
        tk_root.update()
        assert view._advanced.winfo_viewable()
        view._advanced_button.invoke()
        tk_root.update()
        assert not view._advanced.winfo_manager()
    assert view._plan == initial and not accepted
    assert (before, sources) == original


def test_default_accept_uses_displayed_result_without_live_apply(preset, tk_root):
    before = preferences()
    original = copy.deepcopy(before)
    view, accepted = preset(before)
    assert view._view == "preview"
    expected = copy.deepcopy(view._plan)
    view._accept_button.invoke()
    tk_root.update()
    assert len(accepted) == 1 and accepted[0]["expected_plan"] == expected
    assert before == original and not view.winfo_exists()


def test_fully_configured_routes_offer_replanning_instead_of_a_noop_wizard(preset, tk_root):
    before = plan_route_preset(preferences(), catalog(), ai_strategy="auto")["draft"]
    before["service_route_modes"]["youtube"] = "direct"
    before["service_profile_bindings"].pop("youtube")
    original = copy.deepcopy(before)
    view, accepted = preset(before)
    assert view._counts == (0, 11, 0)
    assert "已保留现有分流" == view._empty_note.cget("text")
    assert view._replan_button.winfo_viewable()
    assert not any(row[0].winfo_manager() for row in view._summary_rows.values())
    assert not view._change_details_button.winfo_manager()
    assert "已有 3 项 AI 自动切换线路保持不变" in view._preview_policy.cget("text")
    assert "AI 固定到" not in view._preview_policy.cget("text")
    assert view._accept_button.cget("state") == "disabled"
    view._replan_button.invoke()
    tk_root.update()
    assert view._view == "preview" and view._replace.get()
    assert view._counts[0] == 4
    assert "将替换" in view._preview_policy.cget("text")
    assert not view._replan_button.winfo_manager()
    assert view._plan["draft"]["service_node_bindings"]["openai"] == "h1"
    assert before == original and not accepted
    view._back_button.invoke()
    assert not accepted and before == original


def test_summary_covers_only_actual_changes_and_keeps_direct_routes(preset, tk_root):
    before = preferences()
    before["service_profile_bindings"] = dict.fromkeys(("openai", "claude", "google_ai"), "home")
    before["service_route_modes"]["youtube"] = "direct"
    view, accepted = preset(before)
    assert not view._summary_rows["ai"][0].winfo_manager()
    targets = view._summary_rows["sites"][3].cget("text")
    assert "YouTube" not in targets and "Google 搜索" in targets
    assert view._plan["draft"]["service_route_modes"]["youtube"] == "direct"
    assert view._counts[:2] == (7, 4)
    assert not accepted


def test_details_disclosure_never_changes_draft_and_restores_order(preset, tk_root):
    view, accepted = preset()
    original = copy.deepcopy(view._plan)
    for _ in range(3):
        view._change_details_button.invoke()
        tk_root.update()
        assert all(tile.winfo_viewable() for tile in view._preview_tiles.values())
        view._change_details_button.invoke()
        tk_root.update()
        assert not any(tile.winfo_manager() for tile in view._preview_tiles.values())
    assert view._plan == original and not accepted


def test_adjustment_back_returns_to_current_results_without_accepting(preset, tk_root):
    view, accepted = preset()
    view._setup_tab.invoke()
    view._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
    view._refresh()
    view._back_button.invoke()
    tk_root.update()
    assert view._view == "preview" and not accepted
    assert "直连" in view._summary_rows["sites"][2].cget("text")
    assert view._reviewed_choices == view._choices()
    view._back_button.invoke()
    assert not view.winfo_exists() and not accepted


def test_invalid_adjustment_can_still_be_cancelled(preset, tk_root):
    view, accepted = preset()
    view._strict_privacy = True
    view._scheme.set(ROUTE_PRESETS["ai_only"]["label"])
    view._refresh()
    tk_root.update()
    assert view._plan is None and view._view == "setup"
    assert "严格隐私" in view._error_detail.cget("text")
    assert view._back_button.cget("text") == "取消"
    view._back_button.invoke()
    assert not view.winfo_exists() and not accepted


@pytest.mark.parametrize("mode", ["unchanged", "missing", "replacement"])
def test_result_states_screenshots(preset, tk_root, mode):
    from tools.ui_visual_audit import capture_window_image
    before = plan_route_preset(preferences(), catalog(), ai_strategy="auto")["draft"]
    view, accepted = preset(before if mode != "missing" else preferences(), sources=[] if mode == "missing" else None)
    if mode == "replacement":
        view._replan_button.invoke()
    view.geometry("760x600")
    until = time.monotonic() + 0.4
    while time.monotonic() < until:
        tk_root.update()
        time.sleep(0.01)
    folder = Path(__file__).resolve().parent / "dist" / "preset-result-first"
    folder.mkdir(parents=True, exist_ok=True)
    capture_window_image(view).save(folder / f"{mode}.png")
    assert view._accept_button.winfo_viewable() and not accepted


def test_collapsed_advanced_keeps_real_automatic_policy_and_ack_required(preset, tk_root):
    view, accepted = preset()
    view._setup_tab.invoke()
    view._advanced_button.invoke()
    view._ai_strategy.set(AI_NODE_STRATEGIES["auto"])
    view._refresh()
    view._advanced_button.invoke()
    tk_root.update()
    assert not view._advanced.winfo_manager()
    assert "可能跨国家" in view._policy_summary.cget("text")
    view._accept_button.invoke()
    tk_root.update()
    assert view._view == "preview" and view._auto_ack_checkbox.winfo_viewable()
    assert view._accept_button.cget("state") == "disabled"
    view._accept()
    assert not accepted
    view._auto_ack_checkbox.toggle()
    view._accept()
    assert len(accepted) == 1 and accepted[0]["ai_strategy"] == "auto"


def test_preserved_automatic_ai_warning_is_visible_without_advanced(preset, tk_root):
    existing = preferences()
    existing["service_profile_bindings"] = {"claude": "home"}
    view, accepted = preset(existing)
    assert not view._advanced.winfo_manager()
    assert "已有 1 项 AI 自动切换线路保持不变" in view._policy_summary.cget("text")
    view._replace_checkbox.toggle()
    tk_root.update()
    assert "替换已有线路、固定节点和候选池" in view._policy_summary.cget("text")
    assert "保持不变" not in view._policy_summary.cget("text")
    assert not accepted and not existing["service_node_bindings"]


def test_review_collapses_only_safe_unchanged_rows_and_keeps_warning_visible(preset, tk_root):
    existing = preferences()
    existing["builtin_sites"] = {"youtube": True}
    existing["service_profile_bindings"] = {"youtube": "dc", "claude": "deleted"}
    existing["service_node_bindings"] = {"youtube": "d1", "claude": "missing"}
    view, accepted = preset(existing)
    tk_root.update()
    assert not view._preview_tiles["youtube"].winfo_manager()
    assert view._preview_tiles["claude"].winfo_viewable()
    assert "订阅已失效" in view._preview_rows["claude"][1].cget("text")
    assert "1 项待处理" in view._status.cget("text")
    assert "1 项保留 / 无需修改" in view._kept_button.cget("text")
    before = copy.deepcopy(view._plan)
    view._kept_button.invoke()
    tk_root.update()
    assert view._preview_tiles["youtube"].winfo_viewable()
    view._kept_button.invoke()
    tk_root.update()
    assert not view._preview_tiles["youtube"].winfo_manager()
    assert view._preview_tiles["claude"].winfo_viewable()
    assert view._plan == before and not accepted


def test_missing_subscriptions_remain_visible_in_compact_review(preset, tk_root):
    view, accepted = preset(sources=[])
    tk_root.update()
    assert view._counts[2] == 11
    assert all(tile.winfo_viewable() for tile in view._preview_tiles.values())
    assert view._kept_button.cget("state") == "disabled"
    assert view._accept_button.cget("state") == "disabled" and not accepted


def test_review_rows_restore_canonical_order_when_ack_and_fold_change(preset, tk_root):
    existing = preferences()
    existing["service_route_modes"] = {"youtube": "direct", "reddit": "direct"}
    view, accepted = preset(existing)
    original_scale = ctk.ScalingTracker.widget_scaling
    try:
        for strategy in ("auto", "fixed", "auto", "fixed"):
            view._ai_strategy.set(AI_NODE_STRATEGIES[strategy])
            view._refresh()
            view._show_view("preview")
            view._kept_button.invoke()
            tk_root.update()
            for scale in (1.5, 1.0):
                ctk.set_widget_scaling(scale)
                tk_root.update()
                actual = [tile for tile in view._preview.pack_slaves()
                          if tile in view._preview_tiles.values()]
                wanted = [tile for key, tile in view._preview_tiles.items()
                          if key in view._preview_warnings
                          or view._show_changes and key in view._preview_attention
                          or view._show_kept and key not in view._preview_attention]
                assert actual == wanted
                assert view._accept_button.winfo_viewable()
        assert not accepted
    finally:
        ctk.set_widget_scaling(original_scale)
        tk_root.update()


@pytest.mark.parametrize("scale", [1.0, 2.5])
def test_compact_preset_screenshots_keep_navigation_visible(preset, tk_root, scale):
    from tools.ui_visual_audit import capture_window_image
    view, accepted = preset()
    original_scale = ctk.ScalingTracker.widget_scaling
    try:
        ctk.set_widget_scaling(scale)
        view.geometry("1040x760")
        folder = Path(__file__).resolve().parent / "dist" / "preset-result-first"
        folder.mkdir(parents=True, exist_ok=True)
        for page in ("setup", "preview"):
            view._show_view(page)
            until = time.monotonic() + 0.4
            while time.monotonic() < until:
                tk_root.update()
                time.sleep(0.01)
            assert view._accept_button.winfo_rooty() + view._accept_button.winfo_height() <= view.winfo_rooty() + view.winfo_height()
            assert view._body._parent_canvas.winfo_height() >= 140
            capture_window_image(view).save(folder / f"{page}-{int(scale * 100)}.png")
        assert not accepted
    finally:
        ctk.set_widget_scaling(original_scale)
        tk_root.update()
