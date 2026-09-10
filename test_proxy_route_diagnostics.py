import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from core import local_proxy, proxy_route_diagnostics as diagnostics, remote_proxy


def rule(kind, payload, route="AI-PROXY", **kwargs):
    return {"type": kind, "payload": payload, "proxy": route, **kwargs}


def snapshot():
    now = datetime.now(timezone.utc)
    data = diagnostics.RouteSnapshot("synthetic", {}, [], captured_at=now)
    data.saved = [rule("DOMAIN-SUFFIX", "openai.com")]
    data.runtime = {"mode": "rule", "rules": data.saved + [rule("MATCH", "", "DIRECT")], "proxies": {
        "AI-PROXY": {"all": ["node"], "now": "node", "testUrl": "https://example.test/health"},
        "node": {"alive": True, "history": [{"time": now.isoformat(), "delay": 60}]},
    }}
    data.labels = {"node": "日本家宽 01"}
    return data


@pytest.mark.parametrize("value,expected", [
    ("HTTPS://API.OpenAI.Com/v1?token=synthetic#fragment", "api.openai.com"),
    ("example.com.:8080/path", "example.com"), ("https://例子.测试", "xn--fsqu00a.xn--0zwm56d"),
    ("[::1]:8080", "::1"), ("2001:db8::1", "2001:db8::1"), ("127.0.0.1", "127.0.0.1"),
])
def test_target_normalization_drops_url_credentials_in_query(value, expected):
    assert diagnostics.target_host(value) == expected


@pytest.mark.parametrize("value", ["", "file:///etc/passwd", "https://user:pass@example.test", "example.com:0",
                                   "https://a.test:65536", "a.test\nb.test", r"https://a.test\@b.test",
                                   "a..test", "*.test", "https://[fe80::1%eth0]", "https://-x.test"])
def test_invalid_urls_do_not_echo_secrets(value):
    with pytest.raises(ValueError) as exc:
        diagnostics.target_host(value)
    assert "user:pass" not in str(exc.value)


@pytest.mark.parametrize("kind", ["DOMAIN-SUFFIX", "DomainSuffix", "domain_suffix"])
def test_suffix_is_label_bounded_and_first_matching_rule_wins(kind):
    rules = [rule("Domain", "api.openai.com", "special"), rule(kind, "openai.com"), rule("MATCH", "", "DIRECT")]
    assert diagnostics.match_rules("api.openai.com", rules).route == "special"
    assert diagnostics.match_rules("sub.openai.com", rules).route == "AI-PROXY"
    assert diagnostics.match_rules("evilopenai.com", rules).route == "DIRECT"


@pytest.mark.parametrize("kind,payload", [("GEOIP", "CN"), ("GEOSITE", "google"), ("ProcessName", "app.exe"),
                                          ("IPCIDR", "192.168.0.0/16"), ("RULE-SET", "demo"), ("NETWORK", "udp")])
def test_unknown_preceding_rules_never_fabricate_final_route(kind, payload):
    match = diagnostics.match_rules("example.test", [rule(kind, payload, "DIRECT"), rule("MATCH", "")])
    assert not match.certain
    assert match.route == "AI-PROXY"


def test_disabled_pass_ip_rules_and_non_rule_modes():
    rules = [rule("DOMAIN", "example.test", "REJECT", extra={"disabled": True}),
             rule("DOMAIN", "example.test", "PASS"), rule("MATCH", "")]
    assert diagnostics.match_rules("example.test", rules).route == "AI-PROXY"
    assert diagnostics.match_rules("example.test", rules, "direct").route == "DIRECT"
    assert diagnostics.match_rules("example.test", rules, "global").route == "GLOBAL"
    assert not diagnostics.match_rules("example.test", rules, "unknown").certain
    rules = [rule("IPCIDR6", "2001:db8::/32", "v6"), rule("IP-CIDR", "203.0.113.0/24", "v4")]
    assert diagnostics.match_rules("2001:db8::12", rules).route == "v6"
    assert diagnostics.match_rules("203.0.113.9", rules).route == "v4"


def test_saved_rule_ownership_matches_deployment_without_subscription_reads(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("saved rule analysis must not read subscriptions")
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", forbidden)
    prefs = {"builtin_sites": {"youtube": True}, "service_profile_bindings": {"openai": "home", "custom": "dc"},
             "service_node_bindings": {}, "custom_targets": [
                 {"id": "api", "kind": "domain", "value": "api.openai.com", "enabled": True},
                 {"id": "net", "kind": "ip-cidr", "value": "203.0.113.0/24", "enabled": True},
             ]}
    original = copy.deepcopy(prefs)
    rules = diagnostics.saved_rules(prefs)
    home = local_proxy._subscription_route_group_name("home")
    dc = local_proxy._subscription_route_group_name("dc", "custom")
    assert diagnostics.match_rules("openai.com", rules).route == home
    assert diagnostics.match_rules("api.openai.com", rules).route == dc
    assert diagnostics.match_rules("203.0.113.1", rules).route == dc
    assert diagnostics.match_rules("youtube.com", rules).route == "AI-PROXY"
    assert prefs == original


def test_health_uses_correct_group_url_and_rejects_expired_future_missing_history():
    now = datetime.now(timezone.utc)
    group = {"testUrl": "https://example.test/health"}
    node = {"alive": True, "history": [{"time": now.isoformat(), "delay": 10}], "extra": {
        group["testUrl"]: {"alive": False, "history": [{"time": now.isoformat(), "delay": 0}]}}}
    assert "此策略组探针失败" in diagnostics.health_summary(group, node, now)
    del node["extra"]
    assert "非此目标专测" in diagnostics.health_summary(group, node, now)
    assert "过期" in diagnostics.health_summary(group, node, now + timedelta(minutes=5))
    assert "不同步" in diagnostics.health_summary(group, node, now - timedelta(minutes=5))
    node["history"][-1]["time"] = "bad"
    assert "时间未知" in diagnostics.health_summary(group, node, now)
    assert "未检测" in diagnostics.health_summary(group, {}, now)


def test_explanation_does_not_leak_url_tokens_or_claim_unobserved_health():
    data = snapshot()
    report = data.explain("https://api.openai.com/v1?token=PRIVATE_QUERY_SECRET")
    assert "PRIVATE_QUERY_SECRET" not in report
    assert "策略组与已保存规则一致" in report
    assert "日本家宽 01" in report
    assert "非此目标专测" in report
    assert "快照已过期" in data.explain("openai.com", data.captured_at + timedelta(minutes=2))
    data.runtime["proxies"]["AI-PROXY"]["now"] = "AI-PROXY"
    data.runtime["proxies"]["AI-PROXY"]["all"] = ["AI-PROXY"]
    assert "循环引用" in data.selected("AI-PROXY")


@pytest.fixture
def controller():
    data = snapshot().runtime
    paths = []
    behavior = {"redirect": False, "bad": False, "change": False, "mixed_port": 17897, "truncated": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if behavior["redirect"]:
                self.send_response(302)
                self.send_header("Location", "/forbidden")
                self.end_headers()
                return
            if self.path == "/configs":
                payload = {"mode": data["mode"], "mixed-port": behavior["mixed_port"]}
            elif self.path == "/rules":
                payload = {"rules": copy.deepcopy(data["rules"])}
                # Normal traffic updates rule counters between reads, not routing.
                payload["rules"][0]["extra"] = {"hitCount": len(paths)}
                if behavior["change"] and paths.count("/rules") > 1:
                    payload["rules"][0]["proxy"] = "DIRECT"
            else:
                payload = {"proxies": copy.deepcopy(data["proxies"])}
                payload["proxies"]["node"]["password"] = "synthetic-secret"
            body = b"[]" if behavior["bad"] else json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body) + (10 if behavior["truncated"] else 0)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, paths, behavior
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_controller_reads_only_allowlisted_loopback_endpoints_and_ignores_proxy_env(controller, monkeypatch):
    port, paths, _ = controller
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    data = diagnostics._read_controller(port)
    assert data["mode"] == "rule"
    assert "synthetic-secret" not in json.dumps(data)
    assert paths == ["/configs", "/rules", "/proxies", "/rules", "/configs"]


@pytest.mark.parametrize("kind", ["redirect", "bad", "change"])
def test_controller_rejects_redirect_malformed_and_changing_config(controller, kind):
    port, paths, behavior = controller
    behavior[kind] = True
    with pytest.raises(Exception):
        diagnostics._read_controller(port)
    assert "/forbidden" not in paths


def test_ssh_reader_uses_same_bounded_static_script_and_no_target_url(monkeypatch):
    calls = []
    def execute(client, command, **kwargs):
        calls.append((command, kwargs))
        return 0, json.dumps(snapshot().runtime), ""
    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(execute_command_with_status=execute))
    assert diagnostics._read_remote_controller(object(), 8890)["mode"] == "rule"
    command, kwargs = calls[0]
    assert diagnostics._CONTROLLER_READER in command
    assert "port = 8890" in command
    assert kwargs["timeout"] == 12 and not kwargs["log_command"]
    assert kwargs["max_output_bytes"] == diagnostics.MAX_BYTES


@pytest.mark.parametrize("remote", [False, True])
def test_unmanaged_proxy_is_not_queried_or_cleaned(monkeypatch, remote):
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: {})
    monkeypatch.setattr(diagnostics.proxy_routing, "load_ssh_routes", lambda _scope: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: [])
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: None)
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda _name: SimpleNamespace(running=False))
    monkeypatch.setattr(diagnostics, "_read_controller", lambda *_args: pytest.fail("unmanaged controller queried"))
    monkeypatch.setattr(diagnostics, "_read_remote_controller", lambda *_args: pytest.fail("unmanaged controller queried"))
    result = diagnostics.load_snapshot("synthetic-ssh" if remote else None)
    assert not result.runtime
    assert "未运行" in result.error
    assert "已保存规则" in result.explain("api.openai.com")


def test_alias_mapping_preserves_same_connection_used_in_multiple_groups(monkeypatch):
    node = {"name": "家宽 01", "type": "http", "server": "example.test", "port": 1234}
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: [{"id": "a"}])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", lambda _: SimpleNamespace(nodes=[SimpleNamespace(node=node)]))
    import yaml
    text = remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + yaml.safe_dump({"proxies": [dict(node, name="one"), dict(node, name="two")]})
    assert diagnostics._node_labels(text) == {"one": "家宽 01", "two": "家宽 01"}


@pytest.mark.parametrize("kind,payload,host,expected", [
    ("DomainKeyword", ".openai.com", "openai.com", "DIRECT"),
    ("DomainKeyword", ".openai.com", "api.openai.com", "AI-PROXY"),
    ("DomainKeyword", "api.", "rapid.example.com", "DIRECT"),
    ("DomainSuffix", ".openai.com", "openai.com", "DIRECT"),
    ("Domain", ".openai.com", "openai.com", "DIRECT"),
])
def test_controller_domain_payload_is_not_rewritten(kind, payload, host, expected):
    result = diagnostics.match_rules(host, [rule(kind, payload), rule("MATCH", "", "DIRECT")])
    assert result.certain and result.route == expected


def test_scoped_bare_ipv6_is_rejected_like_scoped_url():
    with pytest.raises(ValueError):
        diagnostics.target_host("fe80::1%eth0")


def test_controller_port_must_belong_to_expected_proxy(controller):
    port, paths, behavior = controller
    assert diagnostics._read_controller(port, expected_mixed_port=17897)["mode"] == "rule"
    paths.clear()
    behavior["mixed_port"] = 7890
    with pytest.raises(ValueError, match="port"):
        diagnostics._read_controller(port, expected_mixed_port=17897)
    assert paths == ["/configs"]


def test_controller_rejects_incomplete_content_length_even_when_json_looks_valid(controller):
    port, _, behavior = controller
    behavior["truncated"] = True
    with pytest.raises(ValueError, match="incomplete"):
        diagnostics._read_controller(port)


def test_oversized_delay_cannot_crash_rendering():
    now = datetime.now(timezone.utc)
    node = {"history": [{"time": now.isoformat(), "delay": 10 ** 500}]}
    assert "无效" in diagnostics.health_summary({}, node, now)


def test_alias_lookup_stops_after_all_deployed_nodes_are_resolved(monkeypatch):
    node = {"name": "家宽 01", "type": "http", "server": "example.test", "port": 1234}
    calls = []
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: [{"id": "a"}, {"id": "unrelated"}])
    def cached(profile):
        calls.append(profile["id"])
        return SimpleNamespace(nodes=[SimpleNamespace(node=node)])
    monkeypatch.setattr(remote_proxy, "load_cached_proxy_subscription", cached)
    import yaml
    text = remote_proxy.AI_PROXY_CONFIG_MARKER + "\n" + yaml.safe_dump({"proxies": [dict(node, name="one")]})
    assert diagnostics._node_labels(text) == {"one": "家宽 01"}
    assert calls == ["a"]


@pytest.mark.parametrize("remote", [False, True])
def test_snapshot_reader_always_verifies_managed_mixed_port(monkeypatch, tmp_path, remote):
    calls = []
    def read(*args, **kwargs):
        calls.append(kwargs)
        return snapshot().runtime
    monkeypatch.setattr(diagnostics, "_read_controller", read)
    monkeypatch.setattr(diagnostics, "_read_remote_controller", read)
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: {})
    monkeypatch.setattr(diagnostics.proxy_routing, "load_ssh_routes", lambda _: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: [])
    monkeypatch.setattr(local_proxy, "_load_state", lambda: {"mixed_port": 17903})
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 12345)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _: True)
    monkeypatch.setattr(local_proxy, "_is_managed_mihomo_pid", lambda *a, **kw: True)
    monkeypatch.setattr(local_proxy, "_managed_local_config_path", lambda *_: tmp_path / "missing.yaml")
    monkeypatch.setattr(remote_proxy, "inspect_ai_proxy", lambda _: SimpleNamespace(running=True, config_path="/synthetic/config.yaml"))
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _: (None, object()))
    monkeypatch.setattr(remote_proxy, "ssh_manager", SimpleNamespace(read_remote_file=lambda *a, **kw: ""))
    result = diagnostics.load_snapshot("synthetic-ssh" if remote else None)
    assert result.runtime and not result.error
    assert calls == [{"expected_mixed_port": 7890 if remote else 17903}]


def test_applied_state_change_during_inspection_invalidates_snapshot(monkeypatch):
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: {})
    monkeypatch.setattr(remote_proxy, "list_proxy_subscription_profiles", lambda: [])
    states = iter([{"applied_config_sha256": "a" * 64}, {"applied_config_sha256": "b" * 64}])
    monkeypatch.setattr(local_proxy, "_load_state", lambda: next(states))
    monkeypatch.setattr(local_proxy, "_read_pid", lambda: 12345)
    monkeypatch.setattr(local_proxy, "_is_pid_running", lambda _: True)
    monkeypatch.setattr(local_proxy, "_is_managed_mihomo_pid", lambda *a, **kw: True)
    monkeypatch.setattr(diagnostics, "_read_controller", lambda *a, **kw: snapshot().runtime)
    result = diagnostics.load_snapshot()
    assert not result.runtime and "发生变化" in result.error
