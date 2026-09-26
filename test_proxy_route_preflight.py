"""Pure rule overlap preview must agree with generated route precedence."""
import copy
from datetime import datetime, timezone

import pytest

from core import proxy_routing, proxy_route_diagnostics
from core.proxy_route_preview import route_preflight
from core.proxy_health import parse_proxy_health, proxy_health_summary
from core.local_proxy_constants import LOCAL_PROXY_BUILTIN_SITES


def test_exact_override_explains_direct_target_using_custom_default():
    preferences = proxy_routing.normalize_routes({
        "builtin_sites": {"youtube": True},
        "service_route_modes": {"youtube": "direct", "custom:own": "default"},
        "custom_targets": [{"id": "own", "value": "youtube.com"}],
    })
    before = copy.deepcopy(preferences)
    preview = route_preflight(preferences)
    assert {"target": "youtube.com", "kind": "override", "shadowed": "youtube", "winner": "custom:own"} in preview["overlaps"]
    assert proxy_route_diagnostics.match_rules("www.youtube.com", proxy_route_diagnostics.saved_rules(preferences)).route == "AI-PROXY"
    assert preferences == before


def test_google_direct_preserves_ai_subdomain_exceptions():
    preferences = {"builtin_sites": {"google": True}, "service_route_modes": {"google": "direct"}}
    preview = route_preflight(preferences)
    assert any(item["target"] == "generativelanguage.googleapis.com" and item["parent"] == "googleapis.com"
               and item["winner"] == "google_ai" and item["shadowed"] == "google" for item in preview["overlaps"])


@pytest.mark.parametrize("cidr,child,host", [("203.0.113.0/24", "203.0.113.0/25", "203.0.113.3"),
                                          ("2001:db8::/32", "2001:db8::/48", "2001:db8::1")])
def test_specific_network_exception_matches_builder(cidr, child, host):
    prefs = proxy_routing.normalize_routes({
        "custom_targets": [{"id": "parent", "value": cidr}, {"id": "child", "value": child}],
        "service_route_modes": {"custom:parent": "default", "custom:child": "direct"},
    })
    assert route_preflight(prefs)["overlaps"] == [{"target": child, "parent": cidr, "kind": "exception",
                                                 "shadowed": "custom:parent", "winner": "custom:child"}]
    assert proxy_route_diagnostics.match_rules(host, proxy_route_diagnostics.saved_rules(prefs)).route == "DIRECT"


def test_same_outbound_and_disabled_direct_are_not_false_conflicts():
    prefs = proxy_routing.normalize_routes({
        "builtin_sites": {"youtube": False, "google": True},
        "service_route_modes": {"youtube": "direct"},
        "custom_targets": [{"id": "same", "value": "google.com"}],
    })
    assert route_preflight(prefs, strict_privacy=True) == {"overlaps": [], "direct": False, "privacy_conflict": False}


def test_overridden_direct_intent_is_not_an_active_privacy_conflict():
    prefs = proxy_routing.normalize_routes({
        "custom_targets": [{"id": "own", "value": "example.test"}],
        "service_route_modes": {"custom": "direct", "custom:own": "default"},
    })
    assert not route_preflight(prefs, strict_privacy=True)["privacy_conflict"]
    prefs["service_route_modes"]["custom:own"] = "direct"
    assert route_preflight(prefs, strict_privacy=True)["privacy_conflict"]
    assert not route_preflight(prefs)["privacy_conflict"]


@pytest.mark.parametrize("url", [item["health_check_url"] for item in LOCAL_PROXY_BUILTIN_SITES])
def test_website_health_is_labeled_http_only(url):
    now = datetime.now(timezone.utc)
    health = parse_proxy_health({"url": url}, {"extra": {url: {"history": [{"time": now.isoformat(), "delay": 10}]}}}, now)
    summary = proxy_health_summary(health)
    assert "目标网站 HTTP 探针通过" in summary and "不代表登录、视频、下载或消息功能可用" in summary


def test_old_generic_probe_is_not_labeled_website_success():
    url = "https://www.gstatic.com/generate_204"
    now = datetime.now(timezone.utc)
    health = parse_proxy_health({"url": url}, {"extra": {url: {"history": [{"time": now.isoformat(), "delay": 10}]}}}, now)
    assert health.target_healthy  # Still a valid generic latency measurement.
    assert "不代表目标网站可用" in proxy_health_summary(health)
