"""AI automatic primary/standby policy using synthetic caches, never live state."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import socket

import pytest

from core import local_proxy, proxy_routing, remote_proxy
from core.route_presets import plan_route_preset


def node(index, name="Synthetic JP", *, region="", **updates):
    return remote_proxy.ProxySubscriptionNode(index, {
        "name": name, "type": "http", "server": f"node{index}.example.test", "port": 8080,
        **updates,
    }, region=region)


def key(item):
    return remote_proxy.proxy_subscription_node_key(item)


def rejected_quality(item, **changes):
    result = remote_proxy.ProxyNodeQualityResult(
        key(item), True, ip_type="VPN 高风险", risk_score=95, quality_score=10,
        confidence="高", coverage_complete=True, classification_basis="信誉源网络/风险字段",
    )
    return replace(result, **changes)


@pytest.fixture(autouse=True)
def isolated_calls(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("AI policy tests must not read/write live caches, proxy state or network")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", forbidden)
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_state", forbidden)
    monkeypatch.setattr(local_proxy, "save_local_proxy_preferences", forbidden)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _profile: {})


@pytest.mark.parametrize("name", ["香港 01", "HK-01", "Hong Kong 01", "🇭🇰 Home", "H.K.G 02"])
def test_hong_kong_name_wins_even_over_non_hk_cached_region(name):
    item = node(1, name, region="日本")
    assert not remote_proxy.proxy_subscription_node_ai_auto_selectable(item)


@pytest.mark.parametrize("region", ["香港", "HK", "Hong Kong"])
def test_hong_kong_region_evidence_blocks_automatic_ai(region):
    assert not remote_proxy.proxy_subscription_node_ai_auto_selectable(node(1, region=region))
    item = node(2)
    assert not remote_proxy.proxy_subscription_node_ai_auto_selectable(
        item, rejected_quality(item, region=region, coverage_complete=False),
    )


def test_hong_kong_server_hint_is_not_hidden_by_cached_region():
    assert not remote_proxy.proxy_subscription_node_ai_auto_selectable(
        node(1, "Synthetic JP", region="日本", server="gateway.example.hk"),
    )


@pytest.mark.parametrize("quality_kind,allowed", [
    ("fresh-rejected", False), ("stale-rejected", True), ("incomplete", True),
    ("unmeasured", True), ("missing", True), ("qualified", True),
])
def test_quality_rejection_requires_fresh_complete_evidence(quality_kind, allowed):
    item = node(1, "Unknown location")
    result = rejected_quality(item)
    if quality_kind == "stale-rejected":
        result = replace(result, checked_at=(datetime.now(timezone.utc) - timedelta(
            seconds=remote_proxy.PROXY_QUALITY_CACHE_TTL_SECONDS + 60)).isoformat())
    elif quality_kind == "incomplete":
        result = replace(result, coverage_complete=False)
    elif quality_kind == "unmeasured":
        result = replace(result, ok=False)
    elif quality_kind == "missing":
        result = None
    elif quality_kind == "qualified":
        result = replace(result, ip_type="家庭住宅", quality_score=90, risk_score=10)
    assert remote_proxy.proxy_subscription_node_ai_auto_selectable(item, result) is allowed


@pytest.mark.parametrize("item", [None, {}, remote_proxy.ProxySubscriptionNode(1, {}),
                                   node(2, **{"dialer-proxy": "another-outbound"})])
def test_auto_gate_rejects_invalid_or_dependent_nodes(item):
    assert not remote_proxy.proxy_subscription_node_ai_auto_selectable(item)


@pytest.mark.parametrize("selected", ["hk", "missing", "", "jp"])
def test_ai_primary_and_standby_both_exclude_hk_and_rejected_quality(monkeypatch, selected):
    hk, jp, us, bad = node(1, "香港"), node(2), node(3, "Synthetic US"), node(4)
    items = (hk, bad, us, jp)
    selected_key = {"hk": key(hk), "missing": "missing-key", "": "", "jp": key(jp)}[selected]
    profile = {"id": "synthetic", "selected_node_key": selected_key}
    cached = remote_proxy.ProxySubscriptionResult(items, "synthetic-not-read")
    qualities = {key(bad): rejected_quality(bad)}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _profile: qualities)
    original = copy.deepcopy((profile, cached))
    primary, fallbacks = local_proxy._selected_subscription_route_pool(profile, ai_sensitive=True, cached=cached)
    assert primary == (jp.node if selected == "jp" else us.node)
    assert {item["server"] for item in (primary, *fallbacks)} == {jp.node["server"], us.node["server"]}
    assert (profile, cached) == original


@pytest.mark.parametrize("reason", ["only-hk", "only-rejected", "only-dependent", "empty"])
def test_no_ai_candidate_stops_instead_of_using_blocked_or_default_node(monkeypatch, reason):
    item = node(1, "香港" if reason == "only-hk" else "Synthetic JP")
    if reason == "only-dependent":
        item = node(1, **{"dialer-proxy": "unavailable-outbound"})
    qualities = {key(item): rejected_quality(item)} if reason == "only-rejected" else {}
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", lambda _profile: qualities)
    cached = remote_proxy.ProxySubscriptionResult(() if reason == "empty" else (item,), "synthetic-not-read")
    with pytest.raises(RuntimeError, match="没有符合 AI|没有可用缓存"):
        local_proxy._selected_subscription_route_pool({"id": "synthetic"}, ai_sensitive=True, cached=cached)


def test_quality_cache_failure_does_not_turn_rejected_nodes_into_unknown(monkeypatch):
    item = node(1)

    def failed_read(_profile):
        raise OSError("synthetic quality snapshot failure")

    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", failed_read)
    with pytest.raises(OSError, match="quality snapshot failure"):
        local_proxy._selected_subscription_route_pool(
            {"id": "synthetic"}, ai_sensitive=True,
            cached=remote_proxy.ProxySubscriptionResult((item,), "synthetic-not-read"),
        )


@pytest.mark.parametrize("manual", ["pin", "pool"])
def test_explicit_manual_hk_or_quality_rejected_nodes_remain_authoritative(monkeypatch, manual):
    hk, bad, other = node(1, "香港"), node(2), node(3, "Synthetic US")
    cached = remote_proxy.ProxySubscriptionResult((hk, bad, other), "synthetic-not-read")
    def no_quality_read(_profile):
        pytest.fail("explicit manual authority must not be re-ranked by automatic quality policy")
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", no_quality_read)
    options = {"node_key": key(hk)} if manual == "pin" else {"node_keys": [key(hk), key(bad)]}
    primary, fallbacks = local_proxy._selected_subscription_route_pool(
        {"id": "synthetic", "selected_node_key": key(other)}, ai_sensitive=True, cached=cached, **options,
    )
    assert primary == hk.node
    assert fallbacks == (() if manual == "pin" else (bad.node,))


def test_non_ai_subscription_primary_and_failover_still_allow_hk(monkeypatch):
    hk, jp = node(1, "香港"), node(2)
    def no_quality_read(_profile):
        pytest.fail("ordinary website routing must not inherit AI quality restrictions")
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_qualities", no_quality_read)
    primary, fallbacks = local_proxy._selected_subscription_route_pool(
        {"id": "synthetic", "selected_node_key": key(hk)}, ai_sensitive=False,
        cached=remote_proxy.ProxySubscriptionResult((jp, hk), "synthetic-not-read"),
    )
    assert primary == hk.node and fallbacks == (jp.node,)


@pytest.mark.parametrize("builder", [local_proxy._local_proxy_fallback_nodes, remote_proxy._remote_proxy_fallback_nodes])
def test_local_remote_standby_use_the_same_ai_gate(builder):
    primary, misleading_hk, quality_hk, rejected, safe = (
        node(1), node(2, "香港", region="日本"), node(3), node(4), node(5, "Synthetic US"),
    )
    qualities = {key(quality_hk): rejected_quality(quality_hk, region="HK", coverage_complete=False),
                 key(rejected): rejected_quality(rejected)}
    assert builder(primary.node, [primary, misleading_hk, quality_hk, rejected, safe], qualities) == (safe.node,)


def test_subscription_refresh_does_not_promote_new_hk_first_node():
    hk, first, replacement = node(1, "香港"), node(2), node(3, "Synthetic US")
    profile = {"id": "synthetic", "selected_node_key": key(first)}
    primary, _ = local_proxy._selected_subscription_route_pool(
        profile, ai_sensitive=True, cached=remote_proxy.ProxySubscriptionResult((hk, first), "synthetic-not-read"),
    )
    assert primary == first.node
    primary, fallbacks = local_proxy._selected_subscription_route_pool(
        profile, ai_sensitive=True, cached=remote_proxy.ProxySubscriptionResult((hk, replacement), "synthetic-not-read"),
    )
    assert primary == replacement.node and not fallbacks


def patch_mixed_subscription(monkeypatch):
    hk, us, jp = node(1, "香港 家宽"), node(2, "Synthetic US"), node(3, "Synthetic JP")
    profile = {"id": "shared", "name": "Synthetic shared source", "network_type": "datacenter",
               "saved_path": "synthetic-not-read", "selected_node_key": key(hk)}
    state = {"profiles": {"shared": profile}, "active_profile_id": "shared"}
    cached = remote_proxy.ProxySubscriptionResult((hk, us, jp), "synthetic-not-read")
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: copy.deepcopy(state))
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda _profile: cached)
    return hk, us, jp


def generated_config(scope, preferences):
    default = node(99, "Synthetic default").node
    if scope == "win":
        content = local_proxy._build_local_mihomo_config(default, 17897, preferences=preferences)
    else:
        # The actual SSH routing adapter accepts an override without reading or
        # writing a host record, connecting, or reloading a running proxy.
        options = proxy_routing.ssh_config_options("synthetic-never-connect", override=preferences)
        content = remote_proxy.build_mihomo_config(default, 7890, **options)
    return remote_proxy.yaml.safe_load(content)


def route_group_members(config, domain):
    route = next(rule.split(",")[2] for rule in config["rules"]
                 if rule.startswith(f"DOMAIN-SUFFIX,{domain},"))
    assert route != "AI-PROXY", "explicit service must use its own managed group"
    group = next(group for group in config["proxy-groups"] if group["name"] == route)
    nodes = {item["name"]: item for item in config["proxies"]}
    return group, [nodes[name]["server"] for name in group["proxies"]]


@pytest.mark.parametrize("scope", ["win", "ssh"])
def test_generated_ai_group_excludes_hk_while_same_source_youtube_keeps_it(monkeypatch, scope):
    hk, us, jp = patch_mixed_subscription(monkeypatch)
    preferences = {"builtin_sites": {"youtube": True},
                   "service_profile_bindings": {"openai": "shared", "youtube": "shared"}}
    before = copy.deepcopy(preferences)
    config = generated_config(scope, preferences)
    ai_group, ai_servers = route_group_members(config, "openai.com")
    chatgpt_group, _ = route_group_members(config, "chatgpt.com")
    youtube_group, youtube_servers = route_group_members(config, "youtube.com")
    assert ai_group["name"] == chatgpt_group["name"] != youtube_group["name"]
    assert ai_servers[0] == us.node["server"]
    assert set(ai_servers) == {us.node["server"], jp.node["server"]}
    assert ai_group["type"] == "fallback"
    assert ai_group["url"] == "https://api.openai.com/v1/models"
    assert youtube_servers[0] == hk.node["server"]
    assert set(youtube_servers) == {hk.node["server"], us.node["server"], jp.node["server"]}
    assert preferences == before


@pytest.mark.parametrize("scope", ["win", "ssh"])
def test_fixed_preset_catalog_recommendation_reaches_safe_final_ai_outbound(monkeypatch, scope):
    hk, us, _jp = patch_mixed_subscription(monkeypatch)
    catalog = proxy_routing.load_route_catalog()
    assert catalog[0]["ai_auto_route_candidate_count"] == 2
    plan = plan_route_preset({}, catalog, "datacenter", ai_strategy="fixed")
    draft = plan["draft"]
    assert draft["service_node_bindings"]["openai"] == key(us)
    before = copy.deepcopy(draft)
    config = generated_config(scope, draft)
    for domain in ("openai.com", "chatgpt.com", "anthropic.com", "gemini.google.com"):
        _group, servers = route_group_members(config, domain)
        assert servers == [us.node["server"]]
    _group, youtube_servers = route_group_members(config, "youtube.com")
    assert youtube_servers[0] == hk.node["server"]
    assert draft == before
