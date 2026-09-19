from contextlib import contextmanager
from concurrent.futures import Future
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy
from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab


def _node(index, node_type="hysteria2"):
    return remote_proxy.ProxySubscriptionNode(index, {
        "name": f"香港-{index}", "type": node_type,
        "server": f"node-{index}.example.test", "port": 443,
        "password": "synthetic-test-password",
    })


def test_data_plane_batch_covers_all_nodes_without_ai_region_or_candidate_limits(monkeypatch):
    nodes = [_node(index) for index in range(24)]
    state = {"active": 0, "peak": 0, "closed": 0}
    lock = threading.Lock()
    started = threading.Barrier(4)

    @contextmanager
    def session(_binary, _node):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            started.wait(timeout=3)
            yield SimpleNamespace(proxy_url="http://127.0.0.1:1234")
        finally:
            with lock:
                state["active"] -= 1
                state["closed"] += 1

    calls = []

    def probe(proxy_url, label, url, timeout):
        calls.append((proxy_url, label, url, timeout))
        return local_proxy.LocalAIProxyProbeResult(label, True, status=204, elapsed_ms=48)

    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("synthetic-mihomo.exe"))
    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)
    monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", probe)

    results = local_proxy.measure_proxy_node_data_plane_latencies(nodes, max_workers=999)

    assert set(results) == {remote_proxy.proxy_subscription_node_key(item) for item in nodes}
    assert all(result.ok and result.latency_ms == 48 for result in results.values())
    assert all(remote_proxy.proxy_node_latency_fresh(result) for result in results.values())
    assert len(calls) == 48
    assert state == {"active": 0, "peak": 4, "closed": 24}
    assert all("实际转发" in result.detail for result in results.values())


def test_data_plane_batch_empty_error_is_recorded_per_node(monkeypatch):
    nodes = [_node(1), _node(2)]
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("synthetic-mihomo.exe"))

    @contextmanager
    def session(_binary, node):
        if node["name"] == "香港-1":
            raise TimeoutError()
        yield SimpleNamespace(proxy_url="http://127.0.0.1:1234")

    monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)
    monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", lambda *args:
                        local_proxy.LocalAIProxyProbeResult(args[1], True, status=204, elapsed_ms=12))
    results = local_proxy.measure_proxy_node_data_plane_latencies(nodes)
    assert len(results) == 2
    assert "TimeoutError" in results[remote_proxy.proxy_subscription_node_key(nodes[0])].detail
    assert results[remote_proxy.proxy_subscription_node_key(nodes[1])].ok


def test_data_plane_missing_core_returns_explicit_results_for_every_node(monkeypatch):
    nodes = [_node(index) for index in range(12)]
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)

    def unavailable():
        raise RuntimeError("synthetic core unavailable")

    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", unavailable)
    results = local_proxy.measure_proxy_node_data_plane_latencies(nodes)
    assert len(results) == len(nodes)
    assert all(not value.ok and "core unavailable" in value.detail for value in results.values())


@pytest.mark.parametrize("failure_stage", ["create", "submit", "iterate", "shutdown"])
def test_data_plane_executor_errors_preserve_completed_results(monkeypatch, failure_stage):
    nodes = [_node(1), _node(2), _node(3)]
    monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
    monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("synthetic-mihomo.exe"))

    class Executor:
        def __init__(self, **kwargs):
            if failure_stage == "create":
                raise RuntimeError("executor unavailable")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            if failure_stage == "shutdown":
                raise RuntimeError("shutdown failure")

        def submit(self, action, key, node):
            if failure_stage == "submit" and node is nodes[1]:
                raise RuntimeError("submit failure")
            result = Future()
            result.set_result(remote_proxy.ProxyNodeLatencyResult(key, True, 35, attempts=2))
            return result

    def failed_iteration(futures):
        yield next(iter(futures))
        raise RuntimeError("iteration failure")

    monkeypatch.setattr(local_proxy, "ThreadPoolExecutor", Executor)
    if failure_stage == "iterate":
        monkeypatch.setattr(local_proxy, "as_completed", failed_iteration)
    results = local_proxy.measure_proxy_node_data_plane_latencies(nodes)
    assert len(results) == 3
    for node in nodes:
        result = results[remote_proxy.proxy_subscription_node_key(node)]
        failed = failure_stage == "create" or (failure_stage == "submit" and node is nodes[1])
        assert result.ok is not failed
        if not failed:
            assert result.latency_ms == 35
        assert remote_proxy.proxy_node_latency_fresh(result)


def test_data_plane_cache_round_trip_keeps_fresh_success_and_failure(monkeypatch):
    nodes = [_node(1), _node(2)]
    results = {
        remote_proxy.proxy_subscription_node_key(node): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_subscription_node_key(node), index == 0,
            latency_ms=53 if index == 0 else None,
            detail="HTTPS 实际转发 2/2；非 TCP 握手延迟" if index == 0 else "实际转发检测失败: TimeoutError",
            attempts=2,
        )
        for index, node in enumerate(nodes)
    }

    def save(profile_id, **updates):
        return {"active_profile_id": profile_id, "profiles": {profile_id: {"id": profile_id, **updates}}}

    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_profile_state", save)
    state = remote_proxy.save_proxy_subscription_latencies(results, profile_id="synthetic-profile")
    restored = remote_proxy.load_proxy_subscription_latencies(state)
    assert set(restored) == set(results)
    for key, source in results.items():
        assert restored[key]["measured_at"] == source.measured_at
        assert remote_proxy.proxy_node_latency_fresh(restored[key])
        assert remote_proxy.proxy_node_latency_label(restored[key]) == ("53ms" if source.ok else "不可连")
        assert restored[key]["detail"] == source.detail


@pytest.mark.parametrize("status,body,expected", [(204, "", True), (200, "portal", False),
                                                 (302, "", False), (204, "unexpected", False)])
def test_data_plane_probe_validates_lightweight_target(status, body, expected):
    ok, _country, _detail = local_proxy._classify_ai_probe_response("HTTPS 连通性", status, body)
    assert ok is expected


def test_tcp_batch_empty_worker_exception_does_not_discard_other_results(monkeypatch):
    nodes = [_node(1, "vless"), _node(2, "vless")]

    def measure(node, *args, **kwargs):
        if node["name"] == "香港-1":
            raise TimeoutError()
        return remote_proxy.ProxyNodeLatencyResult(remote_proxy.proxy_node_key(node), True, 23)

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latency", measure)
    results = remote_proxy.measure_proxy_node_latencies(nodes)
    assert len(results) == 2
    assert "TimeoutError" in results[remote_proxy.proxy_subscription_node_key(nodes[0])].detail
    assert results[remote_proxy.proxy_subscription_node_key(nodes[1])].ok


def test_remote_missing_result_does_not_look_untested(monkeypatch):
    nodes = [_node(1, "vless"), _node(2, "vless")]
    key = remote_proxy.proxy_subscription_node_key(nodes[0])
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda _: (None, object()))
    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", lambda *args, **kwargs:
                        (0, f"latency\t{key}\t1\t23\t\t2\n", ""))
    results = remote_proxy.measure_proxy_node_latencies_on_server("synthetic", nodes)
    assert len(results) == 2
    missing = results[remote_proxy.proxy_subscription_node_key(nodes[1])]
    assert not missing.ok and "未返回" in missing.detail
    assert remote_proxy.proxy_node_latency_label(missing) != "未测"


class _ImmediateThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.mark.parametrize("all_nodes", [False, True])
@pytest.mark.parametrize("failed_group", ["", "tcp", "udp"])
def test_local_batch_publishes_all_results_before_slow_ai_gate_and_saves_udp(monkeypatch, all_nodes, failed_group):
    nodes = [_node(1, "vless"), *[_node(index) for index in range(2, 15)]]
    results = {remote_proxy.proxy_subscription_node_key(node):
               remote_proxy.ProxyNodeLatencyResult(remote_proxy.proxy_subscription_node_key(node), True, 45)
               for node in nodes}
    calls = {"saved": [], "rendered": [], "udp": [], "status": []}
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *args, **kwargs: None)
    def tcp(items, **kwargs):
        if failed_group == "tcp":
            raise RuntimeError("TCP synthetic scheduling failure")
        return {remote_proxy.proxy_subscription_node_key(item): results[remote_proxy.proxy_subscription_node_key(item)]
                for item in items}

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", tcp)

    def udp(items, **_kwargs):
        calls["udp"].extend(items)
        if failed_group == "udp":
            raise RuntimeError("UDP synthetic scheduling failure")
        return {remote_proxy.proxy_subscription_node_key(item): results[remote_proxy.proxy_subscription_node_key(item)]
                for item in items}

    monkeypatch.setattr(local_proxy, "measure_proxy_node_data_plane_latencies", udp)
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_latencies", lambda values, **kwargs:
                        calls["saved"].append((dict(values), kwargs)))
    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._subscription_nodes = nodes
    tab._latency_results = {}
    tab._quality_results = {}
    tab._saved_subscription_load_generation = 1
    tab._subscription_batch_nodes = lambda: nodes[:1] if all_nodes else nodes
    tab._subscription_batch_scope_label = lambda: "全部 14 个节点"
    tab._current_subscription_profile_id = lambda: "synthetic-profile"
    tab._selected_subscription_node_key = lambda: "original"
    tab._set_busy = lambda value: setattr(tab, "_busy", value)
    tab._set_status = lambda message, *args: calls["status"].append(message)
    tab._set_subscription_nodes = lambda *args, **kwargs: calls["rendered"].append(dict(tab._latency_results))
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()

    def stability(*args, **kwargs):
        assert len(calls["rendered"]) == 1
        assert set(calls["rendered"][0]) == set(results)
        assert tab._busy
        return None, {}

    monkeypatch.setattr(local_proxy, "select_stable_local_proxy_node", stability)
    tab._verify_subscription_stability(all_nodes=all_nodes)

    assert calls["udp"] == nodes[1:]
    actual = calls["rendered"][0]
    for node in nodes:
        key = remote_proxy.proxy_subscription_node_key(node)
        failed = (failed_group == "tcp" and node is nodes[0]) or (failed_group == "udp" and node is not nodes[0])
        if failed:
            assert not actual[key].ok and "synthetic scheduling failure" in actual[key].detail
            assert remote_proxy.proxy_node_latency_incomplete(actual[key])
            assert not remote_proxy.proxy_node_latency_explicitly_unreachable(actual[key])
        else:
            assert actual[key] is results[key]
    assert calls["saved"] == [(actual, {"profile_id": "synthetic-profile"})]
    assert calls["rendered"] == [actual]
    assert not tab._busy
    assert any("14/14" in text for text in calls["status"])
    successful = 13 if failed_group == "tcp" else 1 if failed_group == "udp" else 14
    assert any(f"可连 {successful}，失败 0，取消 0，未完成 {14 - successful}" in text for text in calls["status"])
    if all_nodes:
        assert any("忽略筛选与勾选" in text for text in calls["status"])


def test_ssh_latency_completion_does_not_overwrite_changed_subscription(monkeypatch):
    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._proxy_subscription_nodes = [_node(1, "vless")]
    tab._proxy_saved_subscription_load_generation = 1
    tab._current_proxy_subscription_profile_id = lambda: "profile-a"
    tab._require_selected_servers = lambda _: ["synthetic"]
    tab._format_server_target = lambda _: "synthetic"
    tab._proxy_subscription_batch_nodes = lambda: tab._proxy_subscription_nodes
    tab._proxy_subscription_batch_scope_label = lambda: "全部"
    messages = []
    tab._set_proxy_status = lambda message, *args: messages.append(message)
    tab.winfo_toplevel = lambda: object()
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *args, **kwargs: None)

    def task(message, run, on_done):
        tab._proxy_saved_subscription_load_generation = 2
        on_done({"ok": True, "result": {"results": {}}})

    tab._run_proxy_ssh_task = task
    tab._measure_proxy_subscription_latencies()
    assert any("页面分组已变更" in text for text in messages)


def test_ssh_all_nodes_action_ignores_picker_filter(monkeypatch):
    nodes = [_node(1, "vless"), _node(2, "vless")]
    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._proxy_subscription_nodes = nodes
    tab._proxy_saved_subscription_load_generation = 1
    tab._current_proxy_subscription_profile_id = lambda: "profile-a"
    tab._require_selected_servers = lambda _: ["synthetic"]
    tab._format_server_target = lambda _: "synthetic"
    tab._proxy_subscription_batch_nodes = lambda: nodes[:1]
    tab._proxy_subscription_batch_scope_label = lambda: "仅筛选 1 个"
    tab._set_proxy_status = lambda *args: None
    captured = []
    tab._measure_proxy_nodes_for_servers = lambda servers, items, **kwargs: captured.append((servers, items, kwargs))
    tab._run_proxy_ssh_task = lambda message, run, on_done: run()
    tab._measure_proxy_subscription_latencies(all_nodes=True)
    assert len(captured) == 1
    servers, measured, options = captured[0]
    assert (servers, measured) == (["synthetic"], tuple(nodes))
    assert options["quick"] is True
    assert isinstance(options["cancel_event"], threading.Event)
    assert callable(options["progress_callback"])


def _ssh_scope_tab(monkeypatch):
    nodes = [_node(1, "vless"), _node(2, "vless")]
    tab = object.__new__(SSHTab)
    tab._proxy_busy = False
    tab._proxy_subscription_nodes = nodes
    tab._proxy_latency_results = {}
    tab._proxy_latency_target_signature = None
    tab._proxy_latency_server_count = 0
    tab._proxy_saved_subscription_load_generation = 1
    tab._current_proxy_subscription_profile_id = lambda: "profile-a"
    tab._selected_server_names = {"server-a"}
    tab._require_selected_servers = lambda _: sorted(tab._selected_server_names)
    tab._format_server_target = lambda names: ",".join(names)
    tab._proxy_subscription_batch_nodes = lambda: nodes
    tab._proxy_subscription_batch_scope_label = lambda: "全部"
    callbacks, renders, messages = [], [], []
    tab._set_proxy_subscription_nodes = lambda *args, **kwargs: renders.append(dict(tab._proxy_latency_results))
    tab._fastest_proxy_subscription_node = lambda _: None
    tab._run_proxy_ssh_task = lambda message, run, on_done: callbacks.append(on_done)
    tab._set_proxy_status = lambda message, *args: messages.append(message)
    tab.winfo_toplevel = lambda: object()
    tab._target_summary_label = None
    tab._target_hint_label = None
    tab._sync_current_button = None
    tab._sync_selected_button = None
    tab._update_proxy_target_label = lambda: None
    monkeypatch.setattr("ui.tabs.ssh_tab.show_toast", lambda *args, **kwargs: None)
    return tab, nodes, callbacks, renders, messages


def _complete_ssh_measurement(callback, source, nodes):
    values = {remote_proxy.proxy_subscription_node_key(node): remote_proxy.ProxyNodeLatencyResult(
        remote_proxy.proxy_subscription_node_key(node), True, 23,
    ) for node in nodes}
    callback({"ok": True, "result": {"results": {source: values}}})


def test_ssh_target_change_immediately_clears_old_source_and_repaints(monkeypatch):
    tab, nodes, callbacks, renders, _messages = _ssh_scope_tab(monkeypatch)
    tab._measure_proxy_subscription_latencies()
    _complete_ssh_measurement(callbacks.pop(), "server-a", nodes)
    assert len(tab._proxy_latency_results) == 2

    tab._selected_server_names = {"server-b"}
    tab._update_target_context_ui(["server-b"])

    assert tab._proxy_latency_results == {}
    assert tab._proxy_latency_server_count == 0
    assert renders[-1] == {}
    assert tab._proxy_latency_target_signature == (("server-b", "", "", ""),)


def test_ssh_old_source_completion_cannot_repopulate_new_target(monkeypatch):
    tab, nodes, callbacks, renders, messages = _ssh_scope_tab(monkeypatch)
    tab._measure_proxy_subscription_latencies()
    tab._selected_server_names = {"server-b"}
    tab._update_target_context_ui(["server-b"])
    _complete_ssh_measurement(callbacks.pop(), "server-a", nodes)

    assert tab._proxy_latency_results == {}
    assert renders == []
    assert "目标服务器已变化" in messages[-1]


def test_ssh_partial_batch_cannot_merge_other_source_measurements(monkeypatch):
    tab, nodes, callbacks, _renders, _messages = _ssh_scope_tab(monkeypatch)
    tab._measure_proxy_subscription_latencies()
    _complete_ssh_measurement(callbacks.pop(), "server-a", nodes)
    tab._selected_server_names = {"server-b"}
    # Also defend programmatic source changes that skipped the UI toggle hook.
    tab._proxy_subscription_batch_nodes = lambda: nodes[:1]
    tab._measure_proxy_subscription_latencies()
    _complete_ssh_measurement(callbacks.pop(), "server-b", nodes[:1])

    assert set(tab._proxy_latency_results) == {remote_proxy.proxy_subscription_node_key(nodes[0])}


def test_ssh_partial_batches_keep_same_source_results(monkeypatch):
    tab, nodes, callbacks, _renders, _messages = _ssh_scope_tab(monkeypatch)
    for node in nodes:
        tab._proxy_subscription_batch_nodes = lambda selected=node: [selected]
        tab._measure_proxy_subscription_latencies()
        _complete_ssh_measurement(callbacks.pop(), "server-a", [node])
    assert len(tab._proxy_latency_results) == 2


def test_ssh_unattributed_win11_cache_is_not_a_remote_result(monkeypatch):
    tab, nodes, _callbacks, renders, _messages = _ssh_scope_tab(monkeypatch)
    tab._proxy_latency_results = {remote_proxy.proxy_subscription_node_key(nodes[0]): {"ok": True, "latency_ms": 4}}
    tab._update_target_context_ui(["server-a"])
    assert tab._proxy_latency_results == {}
    assert renders == [{}]


@pytest.mark.parametrize("in_flight", [False, True])
def test_ssh_renamed_endpoint_under_same_alias_invalidates_results(monkeypatch, in_flight):
    tab, nodes, callbacks, _renders, messages = _ssh_scope_tab(monkeypatch)
    tab._server_profile_map = {"server-a": SimpleNamespace(host="old.example.test", port=22, username="user")}
    tab._measure_proxy_subscription_latencies()
    if not in_flight:
        _complete_ssh_measurement(callbacks.pop(), "server-a", nodes)
        assert tab._proxy_latency_results

    tab._server_profile_map["server-a"] = SimpleNamespace(host="new.example.test", port=22, username="user")
    tab._update_target_context_ui(["server-a"])
    assert tab._proxy_latency_results == {}
    if in_flight:
        _complete_ssh_measurement(callbacks.pop(), "server-a", nodes)
        assert tab._proxy_latency_results == {}
        assert "目标服务器已变化" in messages[-1]


@pytest.mark.parametrize("failure_stage", ["create", "submit", "iterate", "shutdown"])
def test_tcp_executor_errors_preserve_completed_results(monkeypatch, failure_stage):
    nodes = [_node(index, "vless") for index in (1, 2, 3)]

    class Executor:
        def __init__(self, **kwargs):
            if failure_stage == "create":
                raise RuntimeError("executor unavailable")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            if failure_stage == "shutdown":
                raise RuntimeError("shutdown failure")

        def submit(self, action, node, *args, **kwargs):
            if failure_stage == "submit" and node["name"] == nodes[1].node["name"]:
                raise RuntimeError("submit failure")
            key = remote_proxy.proxy_node_key(node)
            result = Future()
            result.set_result(remote_proxy.ProxyNodeLatencyResult(key, True, 35, attempts=2))
            return result

    def failed_iteration(futures):
        yield next(iter(futures))
        raise RuntimeError("iteration failure")

    monkeypatch.setattr(remote_proxy, "ThreadPoolExecutor", Executor)
    if failure_stage == "iterate":
        monkeypatch.setattr(remote_proxy, "as_completed", failed_iteration)
    results = remote_proxy.measure_proxy_node_latencies(nodes)
    assert len(results) == 3
    for node in nodes:
        result = results[remote_proxy.proxy_subscription_node_key(node)]
        failed = failure_stage == "create" or (failure_stage == "submit" and node is nodes[1])
        assert result.ok is not failed
        if not failed:
            assert result.latency_ms == 35
        assert remote_proxy.proxy_node_latency_fresh(result)
