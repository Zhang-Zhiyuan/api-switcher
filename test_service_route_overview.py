import copy

import customtkinter as ctk
import pytest

from core import proxy_routing
from test_service_routes_dialog import _catalog, _preferences, tk_root as tk_root
from ui.widgets.service_route_overview import ServiceRouteOverview, route_description


def _describe(service, preferences):
    row = next(row for row in proxy_routing.route_rows(preferences) if row["id"] == service)
    return route_description(row, preferences, _catalog())


def test_overview_distinguishes_fixed_automatic_default_and_disabled_routes():
    prefs = _preferences()
    assert _describe("claude", prefs)["hint"] == "固定节点 · 不自动换出口"
    assert _describe("google_ai", prefs)["node"] == "沿用默认节点策略"
    prefs["service_node_bindings"].pop("claude")
    assert _describe("claude", prefs)["hint"] == "自动切换 · 仅限此订阅"
    prefs["builtin_sites"]["youtube"] = False
    description = _describe("youtube", prefs)
    assert description["profile"] == "机房订阅 B"
    assert description["enabled"] is False
    assert "不新增专属规则" in description["hint"]
    assert "直连" not in description["hint"]


@pytest.mark.parametrize("binding,expected", [("bad-profile", "订阅已失效"), ("home", "固定节点已失效")])
def test_invalid_routes_remain_visible_and_do_not_claim_fallback(binding, expected):
    prefs = _preferences()
    prefs["service_profile_bindings"]["claude"] = binding
    prefs["service_node_bindings"]["claude"] = "bad-node"
    assert _describe("claude", prefs)["warning"]
    assert expected in _describe("claude", prefs)["hint"]


def test_custom_target_summary_resolves_inherited_profile_and_fixed_node():
    prefs = _preferences()
    prefs["custom_targets"] = [{"id": "test", "value": "api.example.com", "enabled": True}]
    prefs["service_profile_bindings"]["custom"] = "dc"
    prefs["service_node_bindings"]["custom"] = "three"
    before = copy.deepcopy(prefs)
    desc = _describe("custom:test", prefs)
    assert desc["profile"] == "机房订阅 B"
    assert desc["node"] == "香港 · 流媒体 01"
    assert "继承自定义默认" in desc["hint"]
    assert prefs == before


@pytest.fixture
def overview(tk_root):
    selected = []
    host = ctk.CTkToplevel(tk_root)
    host.geometry("1080x750")
    widget = ServiceRouteOverview(host, command=selected.append)
    widget.pack(fill="x", padx=12, pady=12)
    widget.set_routes(_preferences(), _catalog())
    tk_root.update()
    try:
        yield widget, selected
    finally:
        host.destroy()


def test_overview_browsing_is_read_only_and_disabled_targets_fold(overview):
    widget, selected = overview
    assert widget._rows["youtube"]["tile"].winfo_manager() == "pack"
    assert widget._rows["github"]["tile"].winfo_manager() == ""
    assert "custom" not in widget._rows
    widget._toggle_inactive()
    assert widget._rows["github"]["tile"].winfo_manager() == "pack"
    widget._toggle_inactive()
    assert selected == []
    widget._rows["claude"]["edit"].invoke()
    assert selected == ["claude"]
    widget.set_enabled(False)
    widget._open("youtube")
    widget._manage.invoke()
    assert selected == ["claude"]


def test_overview_equal_refresh_reuses_rows_and_missing_disabled_route_stays_visible(overview):
    widget, _ = overview
    rows = {key: row["tile"] for key, row in widget._rows.items()}
    widget.set_routes(copy.deepcopy(_preferences()), _catalog())
    assert rows == {key: row["tile"] for key, row in widget._rows.items()}
    prefs = _preferences()
    prefs["service_profile_bindings"]["github"] = "deleted"
    widget.set_routes(prefs, _catalog())
    assert widget._rows["github"]["tile"].winfo_manager() == "pack"
    assert "1 项需修复" in widget._summary.cget("text")
    assert rows == {key: row["tile"] for key, row in widget._rows.items()}


def test_overview_switches_between_columns_and_stacked_without_recreating_widgets(overview):
    widget, _ = overview
    row = widget._rows["claude"]
    widget._layout(False)
    assert row["profile"].grid_info()["column"] == 1
    assert widget._heading.winfo_manager() == "pack"
    widget._layout(True)
    assert row["profile"].grid_info()["row"] == 1
    assert row["profile"].grid_info()["columnspan"] == 2
    assert widget._heading.winfo_manager() == ""
    widget._layout(False)
    assert row["profile"].grid_info()["row"] == 0


def test_overview_canvas_resize_uses_dpi_adjusted_width(overview):
    from types import SimpleNamespace
    widget, _ = overview
    scale = widget._get_widget_scaling()
    widget._on_resize(SimpleNamespace(widget=widget._canvas, width=1000 * scale))
    assert widget._narrow is False
    widget._on_resize(SimpleNamespace(widget=widget._canvas, width=600 * scale))
    assert widget._narrow is True
