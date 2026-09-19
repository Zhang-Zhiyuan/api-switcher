"""SSH quick-test UI contracts using in-memory nodes and no Tk/network."""
import json
import threading
from types import MethodType, SimpleNamespace

import pytest

from core import remote_proxy
from ui.tabs import ssh_tab as module


def node(index):
    return remote_proxy.ProxySubscriptionNode(index, {
        "name": f"synthetic-{index}", "type": "http",
        "server": f"node-{index}.example.test", "port": 8080,
    })


def result(item, *, ok=True, cancelled=False):
    return remote_proxy.ProxyNodeLatencyResult(
        remote_proxy.proxy_subscription_node_key(item), ok and not cancelled,
        latency_ms=12 if ok and not cancelled else None,
        detail="已取消" if cancelled else "synthetic",
        cancelled=cancelled,
    )


class Button:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


def view(monkeypatch, servers=("a",)):
    items = [node(1), node(2)]
    obj = SimpleNamespace(
        _proxy_busy=False, _ssh_busy=False, _destroyed=False,
        _proxy_subscription_nodes=items, _proxy_latency_results={}, _proxy_quality_results={},
        _proxy_latency_target_signature=None, _proxy_latency_server_count=0,
        _proxy_saved_subscription_load_generation=1, _selected_server_names=set(servers),
        _proxy_subscription_picker=object(), _proxy_quality_cancel_button=Button(),
        _proxy_quality_cancel_event=None, _proxy_latency_cancel_event=None,
        _proxy_latency_progress=None, _proxy_prefer_quality_sort=False,
    )
    for name in ("_measure_proxy_subscription_latencies", "_proxy_latency_source_signature",
                 "_sync_proxy_latency_target_context", "_measure_proxy_nodes_for_servers",
                 "_aggregate_proxy_latency_results", "_cancel_proxy_subscription_quality"):
        setattr(obj, name, MethodType(getattr(module.SSHTab, name), obj))
    obj.messages, obj.renders, obj.callbacks, obj.tasks = [], [], [], []
    obj.severities = []
    obj.owner = threading.get_ident()
    obj._current_proxy_subscription_profile_id = lambda: "profile-a"
    obj._selected_proxy_subscription_node_key = lambda: "original-key"
    obj._require_selected_servers = lambda _: sorted(obj._selected_server_names)
    obj._format_server_target = lambda names: ",".join(names)
    obj._proxy_subscription_batch_nodes = lambda: items
    obj._proxy_subscription_batch_scope_label = lambda: "全部 2 个节点"
    obj._set_proxy_busy = lambda busy: setattr(obj, "_proxy_busy", busy)

    def status(message, *args):
        assert threading.get_ident() == obj.owner
        obj.messages.append(message)
        obj.severities.append(args[0] if args else "info")

    def render(*args, **kwargs):
        assert threading.get_ident() == obj.owner
        obj.renders.append((dict(obj._proxy_latency_results), kwargs))

    obj._set_proxy_status, obj._set_proxy_subscription_nodes = status, render
    obj._run_on_ui_thread = obj.callbacks.append
    obj.winfo_toplevel = lambda: object()

    def task(message, worker, on_done):
        obj._set_proxy_busy(True)
        obj.tasks.append((worker, on_done))
        return True

    obj._run_proxy_ssh_task = task
    monkeypatch.setattr(module, "show_toast", lambda *args, **kwargs: None)
    for name in ("_select_proxy_subscription_node_by_key", "_use_selected_proxy_subscription_node"):
        setattr(obj, name, lambda *args, **kwargs: pytest.fail("quick tests must not change selection"))
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_latencies",
                        lambda *args, **kwargs: pytest.fail("remote results must not overwrite Win11 cache"))
    return obj


def complete(obj):
    worker, done = obj.tasks.pop(0)
    payload = worker()
    obj._set_proxy_busy(False)
    done({"ok": True, "result": payload})
    for callback in obj.callbacks:
        callback()
    obj.callbacks.clear()


def test_ui_requests_single_quick_attempt_and_keeps_selection(monkeypatch):
    obj = view(monkeypatch)
    calls = []

    def measure(name, items, **kwargs):
        calls.append((name, kwargs))
        return {value.node_key: value for value in map(result, items)}

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", measure)
    obj._measure_proxy_subscription_latencies()
    complete(obj)
    assert calls[0][1]["quick"] is True
    assert calls[0][1]["attempts"] == 1
    assert calls[0][1]["max_workers"] == 32
    assert len(obj._proxy_latency_results) == 2
    assert obj.renders[-1][1] == {"preserve_key": "original-key"}
    assert "未自动更换或保存" in obj.messages[-1]
    assert obj._proxy_latency_cancel_event is None and obj._proxy_latency_progress is None


def test_default_server_helper_keeps_legacy_probe_settings(monkeypatch):
    obj = view(monkeypatch)
    calls = []
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server",
                        lambda *args, **kwargs: calls.append(kwargs) or {})
    obj._measure_proxy_nodes_for_servers(["a"])
    assert calls == [{"timeout": 3.0, "attempts": 2, "max_workers": 32}]


def test_cancel_before_remote_work_marks_every_node_and_never_connects(monkeypatch):
    obj = view(monkeypatch)
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server",
                        lambda *args, **kwargs: pytest.fail("cancelled task must not connect"))
    obj._measure_proxy_subscription_latencies()
    obj._cancel_proxy_subscription_quality()
    assert "等待当前 SSH 建连或远端小批" in obj.messages[-1]
    assert obj._proxy_quality_cancel_button.options["state"] == "disabled"
    complete(obj)
    assert len(obj._proxy_latency_results) == 2
    assert all(remote_proxy.proxy_node_latency_cancelled(value) for value in obj._proxy_latency_results.values())
    assert all(not remote_proxy.proxy_node_latency_explicitly_unreachable(value)
               for value in obj._proxy_latency_results.values())
    assert "失败 0，取消 2" in obj.messages[-1]


def test_cancel_after_partial_batch_keeps_completed_terminal_result(monkeypatch):
    obj = view(monkeypatch)

    def measure(_name, items, **kwargs):
        first = result(items[0])
        kwargs["progress_callback"](1, len(items), first)
        kwargs["cancel_event"].set()
        return {first.node_key: first}

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", measure)
    obj._measure_proxy_subscription_latencies()
    complete(obj)
    values = list(obj._proxy_latency_results.values())
    assert sum(remote_proxy.proxy_node_latency_ok(value) for value in values) == 1
    assert sum(remote_proxy.proxy_node_latency_cancelled(value) for value in values) == 1
    assert "可连 1，失败 0，取消 1" in obj.messages[-1]


def test_remote_failure_preserves_streamed_success_and_fills_missing(monkeypatch):
    obj = view(monkeypatch)

    def measure(_name, items, **kwargs):
        kwargs["progress_callback"](1, len(items), result(items[0]))
        raise TimeoutError()

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", measure)
    obj._measure_proxy_subscription_latencies()
    complete(obj)
    values = obj._proxy_latency_results
    assert remote_proxy.proxy_node_latency_ok(values[result(obj._proxy_subscription_nodes[0]).node_key])
    other = values[result(obj._proxy_subscription_nodes[1]).node_key]
    assert not other.ok and "TimeoutError" in other.detail
    assert not remote_proxy.proxy_node_latency_cancelled(other)


def test_real_quick_entry_keeps_other_server_success_and_warns_for_ssh_failure(monkeypatch):
    obj = view(monkeypatch, servers=("a", "b"))
    calls = []
    monkeypatch.setattr(remote_proxy, "_connect_ssh", lambda name, **kwargs: (None, name))
    monkeypatch.setattr(remote_proxy, "_build_remote_latency_command", lambda *args, **kwargs: "synthetic command")

    def execute(client, _command, **kwargs):
        calls.append(client)
        assert kwargs["log_command"] is False
        if client == "b":
            raise OSError("synthetic SSH command failure")
        nodes = json.loads(kwargs["input_data"])
        return 0, "".join(f"latency\t{item['key']}\t1\t23\t\t1\n" for item in nodes), ""

    monkeypatch.setattr(remote_proxy.ssh_manager, "execute_command_with_status", execute)
    # Exercise the real quick core entry, not a stand-in that merely raises.
    obj._measure_proxy_subscription_latencies()
    complete(obj)
    assert sorted(calls) == ["a", "b"]
    assert len(obj._proxy_latency_results) == 2
    assert all(value.ok and "1/2 可用" in value.detail for value in obj._proxy_latency_results.values())
    assert obj.severities[-1] == "warning"
    assert "部分服务器失败" in obj.messages[-1]
    assert "b: synthetic SSH command failure" in obj.messages[-1]
    assert obj._proxy_latency_cancel_event is None and not obj._proxy_busy


def test_executor_failure_keeps_streamed_results_and_marks_remaining_nodes(monkeypatch):
    obj = view(monkeypatch)

    def measure(_name, items, **kwargs):
        first = result(items[0])
        kwargs["progress_callback"](1, len(items), first)
        return {first.node_key: first}

    def failed_executor(names, action):
        action(names[0])
        raise RuntimeError("synthetic executor failure")

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server", measure)
    monkeypatch.setattr(module, "_run_parallel_server_actions", failed_executor)
    obj._measure_proxy_subscription_latencies()
    complete(obj)
    assert len(obj._proxy_latency_results) == 2
    assert sum(value.ok for value in obj._proxy_latency_results.values()) == 1
    assert "失败 0，取消 0，未完成 1" in obj.messages[-1] and "synthetic executor failure" in obj.messages[-1]
    assert sum(remote_proxy.proxy_node_latency_incomplete(value) for value in obj._proxy_latency_results.values()) == 1


def test_busy_cancel_button_switches_between_latency_and_quality_events(monkeypatch):
    obj = object.__new__(module.SSHTab)
    # Only cancellation controls exist; unrelated proxy buttons are absent.
    for name in ("_proxy_fetch_button", "_proxy_latency_button", "_proxy_quality_button",
                 "_proxy_use_node_button", "_proxy_hot_update_button", "_proxy_quality_settings_button",
                 "_proxy_ping0_button", "_proxy_load_file_button", "_proxy_deploy_button", "_proxy_inspect_button",
                 "_proxy_remote_test_button", "_proxy_remote_cleanup_button",
                 "_proxy_subscription_profile_save_button", "_proxy_subscription_profile_delete_button",
                 "_proxy_auto_refresh_check", "_proxy_periodic_update_check", "_proxy_subscription_picker",
                 "_proxy_subscription_profile_combo", "_proxy_subscription_name_entry", "_proxy_subscription_entry"):
        setattr(obj, name, None)
    obj._proxy_subscription_options = {}
    obj._proxy_quality_cancel_button = Button()
    obj._proxy_quality_cancel_event = None
    obj._proxy_latency_cancel_event = threading.Event()
    obj._update_proxy_subscription_profile_form_controls = lambda: None
    obj._set_proxy_busy(True)
    assert obj._proxy_quality_cancel_button.options == {"state": "normal", "text": "取消测速"}
    obj._proxy_latency_cancel_event.set()
    obj._set_proxy_busy(True)
    assert obj._proxy_quality_cancel_button.options["state"] == "disabled"
    obj._proxy_latency_cancel_event = None
    obj._proxy_quality_cancel_event = threading.Event()
    obj._set_proxy_busy(True)
    assert obj._proxy_quality_cancel_button.options == {"state": "normal", "text": "取消检测"}
    obj._set_proxy_busy(False)
    assert obj._proxy_quality_cancel_button.options["state"] == "disabled"


def test_progress_waits_for_every_server_before_painting_failure(monkeypatch):
    obj = view(monkeypatch, servers=("a", "b"))
    obj._measure_proxy_subscription_latencies()
    progress = obj._proxy_latency_progress
    key = result(obj._proxy_subscription_nodes[0]).node_key
    progress.update(("a", key), result(obj._proxy_subscription_nodes[0], ok=False))
    obj.callbacks.pop(0)()
    assert obj.renders == [] and "1/4" in obj.messages[-1]
    progress.update(("b", key), result(obj._proxy_subscription_nodes[0]))
    progress._flush(force=True)
    assert obj._proxy_latency_results[key].ok
    assert len(obj._proxy_latency_results) == 1
    progress.close()


@pytest.mark.parametrize("changed", ["profile", "target", "destroyed"])
def test_queued_progress_and_completion_cannot_restore_old_context(monkeypatch, changed):
    obj = view(monkeypatch)
    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies_on_server",
                        lambda _name, items, **kwargs: {value.node_key: value for value in map(result, items)})
    obj._measure_proxy_subscription_latencies()
    progress = obj._proxy_latency_progress
    value = result(obj._proxy_subscription_nodes[0])
    progress.update(("a", value.node_key), value)
    if changed == "profile":
        obj._proxy_saved_subscription_load_generation += 1
    elif changed == "target":
        obj._selected_server_names = {"b"}
        obj._sync_proxy_latency_target_context(["b"])
    else:
        obj._destroyed = True
    obj.callbacks.pop(0)()
    assert not obj._proxy_latency_results
    if changed != "destroyed":
        complete(obj)
        assert not obj._proxy_latency_results
    else:
        progress.close()


def test_aggregate_partial_cancel_does_not_become_global_failure(monkeypatch):
    obj = view(monkeypatch)
    item = obj._proxy_subscription_nodes[0]
    good, cancelled = result(item), result(item, cancelled=True)
    key = good.node_key
    values = obj._aggregate_proxy_latency_results({"a": {key: good}, "b": {key: cancelled}}, 2, [item])
    assert values[key].ok and not values[key].cancelled
    assert "1/2 可用" in values[key].detail and "1 台取消" in values[key].detail
    failed = result(item, ok=False)
    values = obj._aggregate_proxy_latency_results({"a": {key: failed}, "b": {key: cancelled}}, 2, [item])
    assert values[key].cancelled and not remote_proxy.proxy_node_latency_explicitly_unreachable(values[key])


def test_thread_start_failure_restores_both_busy_states_and_cancel_control(monkeypatch):
    obj = view(monkeypatch)
    obj._remote_inspect_button = obj._remote_pull_button = None
    obj._remote_pull_options = {}
    obj._set_sync_status = lambda *args: None
    obj._run_proxy_ssh_task = MethodType(module.SSHTab._run_proxy_ssh_task, obj)
    obj._run_ssh_task = MethodType(module.SSHTab._run_ssh_task, obj)

    class FailedThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            raise RuntimeError("synthetic resource limit")

    monkeypatch.setattr(module.threading, "Thread", FailedThread)
    obj._measure_proxy_subscription_latencies()
    assert not obj._proxy_busy and not obj._ssh_busy
    assert obj._proxy_latency_cancel_event is None and obj._proxy_latency_progress is None
    assert obj._proxy_quality_cancel_button.options["state"] == "disabled"
    assert "工作线程启动失败" in obj.messages[-1]


def test_destroy_signals_cancel_and_closes_progress_without_shared_connection_work(monkeypatch):
    obj = object.__new__(module.SSHTab)
    obj._proxy_latency_cancel_event = threading.Event()
    obj._proxy_quality_cancel_event = None
    obj._server_refresh_generation = 1
    obj._responsive_after_id = None
    closed = []
    obj._proxy_latency_progress = SimpleNamespace(close=lambda: closed.append(True))
    for name in ("_cancel_initial_after_callbacks", "_cancel_server_render", "_cancel_server_resume_render",
                 "_cancel_server_refresh_finish", "_cancel_proxy_startup_refresh", "_cancel_proxy_periodic_update"):
        setattr(obj, name, lambda: None)
    monkeypatch.setattr(module.ctk.CTkScrollableFrame, "destroy", lambda self: None)
    obj.destroy()
    assert obj._destroyed and obj._proxy_latency_cancel_event.is_set() and closed == [True]
