"""Bound route observations are bounded and never have mutation authority."""
import copy
import io
import json
import socket
import threading
import time
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy, service_route_verification as verification
from core.proxy_update_result import update_result


def preferences():
    return {"service_profile_bindings": {"openai": "a", "youtube": "b", "custom": "c"},
            "builtin_sites": {"youtube": True}, "service_route_modes": {"google": "direct"}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("This test may not access a real socket")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def test_targets_cover_all_bound_sources_but_never_import_custom_addresses():
    prefs = preferences()
    prefs["custom_targets"] = [{"value": "https://secret.invalid/?key=never-copy"}]
    rows = verification.bound_route_targets(prefs)
    assert {row["service"] for row in rows} == {"openai", "youtube", "google", "custom"}
    assert {row["service"] for row in rows if not row["reason"]} == {"openai", "youtube"}
    assert "never-copy" not in json.dumps(rows)


def test_parallel_deadline_reports_each_target_without_waiting_for_stuck_probe():
    targets = verification.bound_route_targets({"service_profile_bindings": {
        service: "source" for service in ("openai", "claude", "google_ai", "youtube", "github")},
        "builtin_sites": {"youtube": True, "github": True}})
    release = threading.Event()
    active, peak = [], []
    lock = threading.Lock()

    def slow(target, timeout):
        with lock:
            active.append(target["service"])
            peak.append(len(active))
        release.wait(1)
        return "passed", "synthetic"

    started = time.monotonic()
    try:
        result = verification.verify_targets(targets, slow, deadline=0.15)
        assert time.monotonic() - started < 0.7
        assert len(result) == 5 and all(row["status"] == "unverified" for row in result)
        assert max(peak) <= 4
    finally:
        release.set()


def test_cancelled_batch_does_not_start_probes():
    called = []
    result = verification.verify_targets(verification.bound_route_targets(preferences()),
                                         lambda *args: called.append(args), cancelled=lambda: True)
    assert not called
    assert all(row["status"] == "unverified" for row in result)


def test_one_failure_does_not_erase_other_successes_or_request_reload():
    targets = verification.bound_route_targets(preferences())
    rows = verification.verify_targets(targets, lambda target, _: (
        "failed" if target["service"] == "openai" else "passed", "synthetic"))
    result = verification.attach_verification(update_result("配置已加载", "applied"), rows)
    assert result.applied and result.warning and not result.retryable
    assert {row["service"]: row["status"] for row in result.route_verification} == {
        "openai": "failed", "youtube": "passed", "custom": "unverified", "google": "unverified"}


def local_fixture(monkeypatch, outcome="applied"):
    prefs = preferences()
    state = {"pid": 42, "mixed_port": 17897}
    fingerprint = ["expected"]
    calls = []
    def reload(*_args, _snapshot, **_kwargs):
        _snapshot.update(preferences=copy.deepcopy(prefs), identity=(42, 17897), port=17897, fingerprint="expected")
        return update_result("synthetic loaded", outcome)
    monkeypatch.setattr(local_proxy, "_reload_bound_local_service_routes", reload)
    monkeypatch.setattr(local_proxy, "_load_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _: True)
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: prefs)
    monkeypatch.setattr(local_proxy, "_managed_local_config_path", lambda _: "synthetic")
    monkeypatch.setattr(local_proxy, "_local_config_sha256", lambda _: fingerprint[0])
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    monkeypatch.setattr(verification, "probe_target", lambda target, port, timeout: (
        calls.append((target["service"], port)) or ("passed", "synthetic")))
    return prefs, state, fingerprint, calls


def test_local_refresh_observes_every_bound_source_on_formal_port(monkeypatch):
    _prefs, _state, _fingerprint, calls = local_fixture(monkeypatch)
    result = local_proxy.refresh_running_local_service_routes_from_subscription([], profile_id="a")
    assert set(calls) == {("openai", 17897), ("youtube", 17897)}
    assert result.applied and len(result.route_verification) == 4


@pytest.mark.parametrize("outcome", ["unchanged", "skipped", "retained", "failed", "unknown"])
def test_local_non_applied_does_not_repeat_network_probe(monkeypatch, outcome):
    _prefs, _state, _fingerprint, calls = local_fixture(monkeypatch, outcome)
    result = local_proxy.refresh_running_local_service_routes_from_subscription([], profile_id="a")
    assert not calls and result.outcome == outcome


@pytest.mark.parametrize("change", ["config", "routes", "process", "shutdown"])
def test_local_concurrent_change_discards_old_observation(monkeypatch, change):
    prefs, state, fingerprint, _calls = local_fixture(monkeypatch)
    def probe(target, port, timeout):
        if change == "config":
            fingerprint[0] = "manual-new"
        elif change == "routes":
            prefs["service_profile_bindings"]["openai"] = "manual-new"
        elif change == "process":
            state["pid"] = 99
        else:
            local_proxy._ISOLATED_MIHOMO_SHUTTING_DOWN.set()
        return "passed", "synthetic"
    monkeypatch.setattr(verification, "probe_target", probe)
    result = local_proxy.refresh_running_local_service_routes_from_subscription([], profile_id="a")
    assert result.applied and all(row["status"] == "unverified" for row in result.route_verification)


def test_remote_reuses_one_client_and_discards_config_race(monkeypatch):
    client = object()
    prefs = preferences()
    config = ["expected"]
    calls = []
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda value: "/synthetic")
    monkeypatch.setattr(remote_proxy.proxy_routing, "load_ssh_routes", lambda name: prefs)
    monkeypatch.setattr(remote_proxy.ssh_manager, "read_remote_file", lambda value, path: config[0])
    def execute(value, command, **kwargs):
        calls.append((value, command, kwargs))
        prefs["service_profile_bindings"]["openai"] = "manual-new"
        return 0, json.dumps([{"service": "openai", "status": "passed", "http_status": 401}]), ""
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", execute)
    result = remote_proxy._verify_remote_bound_route_refresh("host", 7890, update_result("loaded", "applied"),
                                                           copy.deepcopy(prefs), {"client": client, "new_config": "expected", "config_path": "/synthetic/.config/mihomo/config.yaml"})
    assert len(calls) == 1 and calls[0][0] is client
    assert calls[0][2] == {"timeout": 12, "log_command": False}
    assert result.applied and all(row["status"] == "unverified" for row in result.route_verification)


@pytest.mark.parametrize("outcome", ["unchanged", "skipped", "failed", "unknown"])
def test_remote_non_applied_never_opens_session(monkeypatch, outcome):
    monkeypatch.setattr(remote_proxy.remote_config, "_remote_home", lambda value: pytest.fail("unexpected SSH"))
    result = remote_proxy._verify_remote_bound_route_refresh("host", 7890, update_result("loaded", outcome),
                                                           preferences(), {"client": object(), "new_config": "expected"})
    assert result.outcome == outcome


@pytest.mark.parametrize("payload", ["[]", "{}", "bad json", '[{"service":"openai","status":"passed"},{"service":"openai","status":"passed"}]'])
def test_missing_or_duplicate_remote_results_are_unverified(payload):
    rows = verification.parse_remote_records(verification.bound_route_targets(preferences()), 0, payload)
    assert all(row["status"] == "unverified" for row in rows)


@pytest.mark.parametrize("service,status,payload", [
    ("openai", 401, {"error": {"message": "API key is required"}}),
    ("openai", 200, {"object": "list", "data": []}),
    ("google_ai", 403, {"error": {"message": "API key blocked in this region"}}),
    ("discord", 200, {"url": "wss://gateway.discord.gg"}),
    ("youtube", 204, ""), ("github", 200, "<html>captive page</html>"),
    ("telegram", 200, "<html>Telegram</html>"),
])
def test_remote_generated_python_executes_against_synthetic_http(monkeypatch, service, status, payload):
    target = verification.bound_route_targets({"service_profile_bindings": {service: "source"},
                                               "builtin_sites": {service: True}})[0]
    body = payload if isinstance(payload, str) else json.dumps(payload)
    calls = []
    class Connection:
        sock = None
        def __init__(self, host, port, **kwargs): calls.append((host, port))
        def set_tunnel(self, host, port): calls.append((host, port))
        def connect(self): pass
        def request(self, method, path, **kwargs):
            assert method == "GET" and "Authorization" not in kwargs.get("headers", {})
        def getresponse(self): return SimpleNamespace(status=status, read=lambda count: body.encode())
        def close(self): pass
    monkeypatch.setattr(verification.http.client, "HTTPSConnection", Connection)
    command = verification.build_remote_command((target,), 17897)
    source = command.split("<<'API_SWITCHER_ROUTE_PROBE'\n", 1)[1].split("\nAPI_SWITCHER_ROUTE_PROBE", 1)[0]
    monkeypatch.setattr("sys.argv", ["probe", json.dumps([target]), "17897"])
    output = io.StringIO()
    with redirect_stdout(output):
        exec(compile(source, "<isolated-route-probe>", "exec"), {})
    rows = verification.parse_remote_records((target,), 0, output.getvalue())
    assert rows[0]["status"] == verification.classify_response(target, status, body)[0]
    assert calls[0] == ("127.0.0.1", 17897)


def test_remote_builder_rejects_user_url_even_if_service_is_builtin():
    target = dict(verification.bound_route_targets(preferences())[2])
    target.update(service="openai", url="https://private.invalid/?key=secret", reason="")
    with pytest.raises(ValueError, match="非内置"):
        verification.build_remote_command((target,), 7890)


def test_expected_skips_and_unchanged_do_not_create_warning(monkeypatch):
    local_fixture(monkeypatch, "unchanged")
    unchanged = local_proxy.refresh_running_local_service_routes_from_subscription([], profile_id="a")
    assert unchanged.applied and not unchanged.warning
    rows = verification.verify_targets(verification.bound_route_targets(preferences()),
                                       lambda *_: ("passed", "synthetic"))
    assert not verification.attach_verification(update_result("loaded", "applied"), rows).warning


@pytest.mark.parametrize("remote", [False, True])
def test_timer_expiring_during_tls_connect_never_sends_request(monkeypatch, remote):
    target = verification.bound_route_targets({"service_profile_bindings": {"openai": "a"}})[0]
    calls, timers = [], []
    class Timer:
        def __init__(self, timeout, callback):
            self.callback = callback
            timers.append(self)
        def start(self): pass
        def cancel(self): pass
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def set_tunnel(self, *args): pass
        def connect(self):
            timers[-1].callback()
            self.sock = SimpleNamespace(shutdown=lambda *_: calls.append("shutdown"))
        def request(self, *_args, **_kwargs): pytest.fail("Expired TLS connection issued a request")
        def close(self): calls.append("close")
    monkeypatch.setattr(threading, "Timer", Timer)
    monkeypatch.setattr(verification.http.client, "HTTPSConnection", Connection)
    if remote:
        monkeypatch.setattr("sys.argv", ["probe", json.dumps([target]), "17897"])
        output = io.StringIO()
        with redirect_stdout(output):
            exec(compile(verification._REMOTE_SCRIPT, "<isolated-route-probe>", "exec"), {})
        result = verification.parse_remote_records((target,), 0, output.getvalue())[0]["status"]
    else:
        result = verification.probe_target(target, 17897)[0]
    assert result == "failed" and "shutdown" in calls


@pytest.mark.parametrize("change", ["config-before", "config-after", "pid-after", "foreign-process"])
def test_remote_script_invalidates_changed_config_or_process(monkeypatch, change):
    import hashlib
    import pathlib

    target = verification.bound_route_targets({"service_profile_bindings": {"openai": "a"}})[0]
    path = "/synthetic/.config/mihomo/config.yaml"
    after = [False]
    expected = hashlib.sha256(b"expected").hexdigest()
    def read_bytes(item):
        if str(item).replace("\\", "/").endswith("config.yaml"):
            return b"changed" if change == "config-before" or change == "config-after" and after[0] else b"expected"
        return b"foreign\x00" if change == "foreign-process" else b"mihomo\x00-f\x00" + path.encode() + b"\x00"
    monkeypatch.setattr(pathlib.Path, "read_bytes", read_bytes)
    monkeypatch.setattr(pathlib.Path, "read_text", lambda *_args, **_kwargs: "43" if change == "pid-after" and after[0] else "42")
    monkeypatch.setattr("os.kill", lambda *_: None)
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def set_tunnel(self, *args): pass
        def connect(self): pass
        def request(self, *args, **kwargs): after[0] = True
        def getresponse(self): return SimpleNamespace(status=401, read=lambda _: b'{"error":{"message":"API key required"}}')
        def close(self): pass
    monkeypatch.setattr(verification.http.client, "HTTPSConnection", Connection)
    # pathlib is platform-specific; keep synthetic proc command-line consistent
    # with the platform's string rendering while never touching a real process.
    command_path = str(pathlib.Path(path))
    def compatible_read_bytes(item):
        value = read_bytes(item)
        return value.replace(path.encode(), command_path.encode())
    monkeypatch.setattr(pathlib.Path, "read_bytes", compatible_read_bytes)
    monkeypatch.setattr("sys.argv", ["probe", json.dumps([target]), "17897", path, expected])
    output = io.StringIO()
    with redirect_stdout(output):
        try:
            exec(compile(verification._REMOTE_SCRIPT, "<isolated-route-probe>", "exec"), {})
        except SystemExit as exc:
            assert exc.code == 0
    assert json.loads(output.getvalue()) == []


def test_local_operation_lock_is_released_before_probe(monkeypatch):
    from contextlib import contextmanager
    held = []
    @contextmanager
    def lock(_operation):
        held.append(True)
        try:
            yield
        finally:
            held.clear()
    monkeypatch.setattr(local_proxy, "_local_proxy_operation_lock", lock)
    monkeypatch.setattr(local_proxy, "local_proxy_service_bindings_for_profile", lambda _: ["openai"])
    prefs = {"service_profile_bindings": {"openai": "a"}, "service_node_bindings": {"openai": "fixed"}}
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: prefs)
    monkeypatch.setattr(local_proxy.proxy_routing, "node_pool_warnings", lambda *args, **kwargs: [])
    monkeypatch.setattr(local_proxy, "apply_local_proxy_routing_to_running", lambda: update_result("loaded", "applied"))
    state = {"pid": 42, "mixed_port": 17897}
    monkeypatch.setattr(local_proxy, "_load_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_managed_local_proxy_is_running", lambda _: True)
    monkeypatch.setattr(local_proxy, "_managed_local_config_path", lambda _: "synthetic")
    monkeypatch.setattr(local_proxy, "_local_config_sha256", lambda _: "expected")
    monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
    def probe(*args):
        assert not held
        return "passed", "synthetic"
    monkeypatch.setattr(verification, "probe_target", probe)
    node = remote_proxy.ProxySubscriptionNode(1, {"type": "http", "name": "synthetic", "server": "127.0.0.1", "port": 9})
    result = local_proxy.refresh_running_local_service_routes_from_subscription([node], profile_id="a")
    assert result.route_verification[0]["status"] == "passed"
