"""AI overview risk/count presentation with pure synthetic route metadata."""
import copy

import pytest

from core import proxy_routing
from ui.widgets.service_route_overview import ServiceRouteOverview, route_description


def catalog(*, eligible=1):
    nodes = [{"key": "blocked", "label": "Synthetic node 1", "region": "日本", "ai_auto_selectable": False}]
    nodes.extend({"key": f"safe-{index}", "label": f"Synthetic JP {index}", "region": "日本",
                  "ai_auto_selectable": True} for index in range(eligible))
    return [{"id": "shared", "name": "Synthetic source", "network_type": "residential",
             "auto_route_candidate_count": len(nodes), "ai_auto_route_candidate_count": eligible,
             "nodes": nodes}]


def preferences(service="openai", *, pin="", pool=()):
    return {"service_profile_bindings": {service: "shared"},
            "service_node_bindings": {service: pin} if pin else {},
            "service_node_pools": {service: list(pool)} if pool else {},
            "builtin_sites": {service: True}}


def describe(service, prefs, sources):
    row = next(row for row in proxy_routing.route_rows(prefs) if row["id"] == service)
    before = copy.deepcopy((prefs, sources))
    result = route_description(row, prefs, sources)
    assert (prefs, sources) == before, "presentation must not repair manual or live routes"
    return result


@pytest.mark.parametrize("service", ["openai", "claude", "google_ai"])
def test_ai_automatic_single_filtered_candidate_does_not_claim_a_backup(service):
    description = describe(service, preferences(service), catalog(eligible=1))
    assert "暂无备用" in description["node"] and "暂无备用" in description["hint"]
    assert "AI 筛选后仅 1" in description["hint"]
    assert "未验证真实出口或连通性" in description["hint"]
    assert not description["warning"]


@pytest.mark.parametrize("service", ["openai", "claude", "google_ai"])
def test_ai_automatic_no_filtered_candidate_exposes_configuration_failure(service):
    description = describe(service, preferences(service), catalog(eligible=0))
    assert description["warning"]
    assert "无符合 AI 自动筛选" in description["node"]
    assert "自动应用将停止" in description["hint"]
    assert "默认线路" not in description["node"]


def test_ai_automatic_multiple_candidates_explains_primary_and_standby_filter():
    description = describe("openai", preferences(), catalog(eligible=2))
    assert description["node"] == "AI 推荐候选 + 故障切换"
    assert "主备按服务策略筛选" in description["hint"]
    assert "未锁定国家" in description["hint"]
    assert not description["warning"]


@pytest.mark.parametrize("selection", ["pin", "pool"])
@pytest.mark.parametrize("metadata", ["known-rejected", "legacy-hk-name", "hk-region"])
def test_manual_ai_risk_is_advisory_and_keeps_original_authority(selection, metadata):
    sources = catalog()
    blocked = sources[0]["nodes"][0]
    if metadata == "legacy-hk-name":
        blocked.pop("ai_auto_selectable")
        blocked["label"] = "香港 HK-01"
    elif metadata == "hk-region":
        blocked["region"] = "HK"
        blocked["ai_auto_selectable"] = True
    prefs = preferences(pin="blocked") if selection == "pin" else preferences(pool=("safe-0", "blocked"))
    description = describe("openai", prefs, sources)
    assert description["warning"]
    assert "已保留原选择" in description["hint"]
    assert "检查实际连通性" in description["hint"]
    assert "固定节点" in description["hint"] if selection == "pin" else "候选优先顺序" in description["hint"]
    assert "自动应用将停止" not in description["hint"]


@pytest.mark.parametrize("selection", ["automatic", "pin", "pool"])
def test_ordinary_website_does_not_inherit_ai_exclusions(selection):
    sources = catalog(eligible=0)
    sources[0]["nodes"][0].update(label="香港", region="香港")
    prefs = preferences("youtube", pin="blocked" if selection == "pin" else "",
                        pool=("blocked",) if selection == "pool" else ())
    description = describe("youtube", prefs, sources)
    assert not description["warning"]
    assert "AI 自动筛选" not in description["hint"]
    assert "暂无备用" in description["hint"] if selection == "automatic" else True


def test_unknown_region_is_not_misrepresented_as_policy_rejection_or_verified_exit():
    sources = catalog(eligible=0)
    sources[0].pop("ai_auto_route_candidate_count")
    sources[0]["nodes"] = [{"key": "unknown", "label": "Unnamed node", "region": "其他"}]
    description = describe("openai", preferences(), sources)
    assert not description["warning"]
    assert "未验证真实出口或连通性" in description["hint"]
    manual = describe("openai", preferences(pin="unknown"), sources)
    assert not manual["warning"]
    assert "不保证固定 IP / 国家" in manual["hint"]


def test_missing_manual_node_keeps_specific_error_instead_of_policy_warning():
    description = describe("openai", preferences(pin="removed"), catalog())
    assert description["warning"] and "固定节点已失效" in description["hint"]
    assert "已保留原选择" not in description["hint"]


class Label:
    def __init__(self):
        self.options = {}

    def cget(self, key):
        return self.options.get(key)

    def configure(self, **options):
        self.options.update(options)

    def pack_forget(self):
        pass


def test_overview_summary_counts_policy_warning_as_needs_check_without_native_widgets():
    widget = object.__new__(ServiceRouteOverview)
    widget._signature, widget._narrow, widget._rows = None, False, {}
    widget._init_runtime()
    widget._runtime_preferences_fingerprint = None
    widget._runtime_status = Label()
    widget._summary = Label()
    widget._build_row = lambda _key: {name: Label() for name in ("target", "state", "profile", "node", "hint", "runtime")}
    widget._layout = lambda *_args: None
    widget._filter = lambda: None
    prefs, sources = preferences(pin="blocked"), catalog()
    widget.set_routes(prefs, sources)
    assert "1 项需检查" in widget._summary.cget("text")
    assert "需修复" not in widget._summary.cget("text")
    sources[0]["nodes"][0]["ai_auto_selectable"] = True
    widget.set_routes(prefs, sources)
    assert "需检查" not in widget._summary.cget("text")
