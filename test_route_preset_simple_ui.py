"""Compact preset presentation preserves explicit review and route safety."""
import copy
from pathlib import Path
import socket
import time

import customtkinter as ctk
import pytest

from core.route_presets import AI_NODE_STRATEGIES
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


def test_default_setup_shows_recommendation_and_keeps_advanced_optional(preset, tk_root):
    before, sources = preferences(), catalog()
    original = copy.deepcopy((before, sources))
    view, accepted = preset(before, sources)
    assert not view._advanced.winfo_manager()
    assert view._replace_checkbox.winfo_viewable()
    assert all(combo.winfo_viewable() for combo in view._source_combos.values())
    assert "固定推荐节点" in view._policy_summary.cget("text")
    assert "保留已有手动分流" in view._policy_summary.cget("text")
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


def test_collapsed_advanced_keeps_real_automatic_policy_and_ack_required(preset, tk_root):
    view, accepted = preset()
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
    view._accept_button.invoke()
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
    view._accept_button.invoke()
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
                          if view._show_kept or key in view._preview_attention]
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
        folder = Path(__file__).resolve().parent / "dist" / "proxy-simple-workflow-v2469" / "preset"
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
