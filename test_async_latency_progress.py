"""Synthetic incremental measurements and bounded UI dispatch, no live traffic."""
from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy
from test_proxy_latency_coverage import _ImmediateThread, _node
from ui.async_progress import CoalescedProgress
from ui.tabs.local_proxy_tab import LocalProxyTab


class _Timer:
    def __init__(self, delay, callback):
        self.delay, self.callback = delay, callback
        self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


def test_progress_coalesces_many_updates_into_one_queued_callback():
    queued, painted = [], []
    clock = [0.0]
    timers = []
    def timer_factory(*args):
        timer = _Timer(*args)
        timers.append(timer)
        return timer
    progress = CoalescedProgress(queued.append, painted.append, clock=lambda: clock[0], timer_factory=timer_factory)
    for index in range(5000):
        progress.update(str(index), index)
    assert len(queued) == 1 and not painted
    queued.pop()()
    assert len(painted) == 1 and len(painted[0]) == 5000
    progress.update("later", 1)
    queued.pop()()
    assert not queued and len(timers) == 1 and timers[0].started
    # No further probe completion is required to paint the last fast result.
    clock[0] = 0.3
    timers[0].callback()
    progress.update("latest", 2)
    assert len(queued) == 1
    queued.pop()()
    assert painted[-1]["later"] == 1 and painted[-1]["latest"] == 2


def test_closing_progress_cancels_trailing_timer_and_rejects_late_timer_callback():
    queued, painted, timers = [], [], []
    def timer_factory(*args):
        timers.append(_Timer(*args))
        return timers[-1]
    progress = CoalescedProgress(queued.append, painted.append, clock=lambda: 0, timer_factory=timer_factory)
    progress.update("one", 1)
    queued.pop()()
    progress.update("two", 2)
    queued.pop()()
    progress.close()
    assert timers[0].cancelled
    timers[0].callback()
    assert not queued and painted == [{"one": 1}]


def test_timer_start_failure_publishes_pending_values_instead_of_sticking():
    queued, painted = [], []
    def fail(*_args):
        raise RuntimeError("synthetic timer allocation failure")
    progress = CoalescedProgress(queued.append, painted.append, clock=lambda: 0, timer_factory=fail)
    progress.update("one", 1)
    queued.pop()()
    progress.update("two", 2)
    queued.pop()()
    assert painted[-1] == {"one": 1, "two": 2}
    assert not progress._pending


def test_closing_progress_discards_queued_updates_before_final_results():
    queued, painted = [], []
    progress = CoalescedProgress(queued.append, painted.append)
    progress.update("old", 1)
    progress.close()
    queued.pop()()
    progress.update("late", 2)
    assert not queued and not painted and not progress._values


def test_failed_dispatch_can_retry_without_losing_completed_values():
    queued, painted = [], []
    attempts = []

    def dispatch(callback):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("synthetic dispatch unavailable")
        queued.append(callback)

    progress = CoalescedProgress(dispatch, painted.append)
    progress.update("one", 1)
    progress.update("two", 2)
    queued.pop()()
    assert painted == [{"one": 1, "two": 2}]


@pytest.mark.parametrize("transport", ["tcp", "data"])
def test_batch_startup_failure_reports_each_terminal_result_once(monkeypatch, transport):
    nodes = [_node(1), _node(2)]
    reported = []

    def fail(*_args, **_kwargs):
        raise TimeoutError()

    if transport == "tcp":
        monkeypatch.setattr(remote_proxy, "ThreadPoolExecutor", fail)
        results = remote_proxy.measure_proxy_node_latencies(nodes, progress_callback=lambda *args: reported.append(args))
    else:
        monkeypatch.setattr(local_proxy, "_ensure_local_dirs", fail)
        results = local_proxy.measure_proxy_node_data_plane_latencies(nodes, progress_callback=lambda *args: reported.append(args))
    assert [item[:2] for item in reported] == [(1, 2), (2, 2)]
    assert {item[2].node_key for item in reported} == set(results)
    assert all(not item.ok and "TimeoutError" in item.detail for item in results.values())


@pytest.mark.parametrize("transport", ["tcp", "data"])
@pytest.mark.parametrize("broken_callback", [False, True])
def test_each_probe_reports_before_slow_peer_finishes_and_callback_errors_are_safe(monkeypatch, transport, broken_callback):
    nodes = [_node(1, "vless" if transport == "tcp" else "hysteria2"), _node(2)]
    released = threading.Event()
    calls = []

    def report(done, total, result):
        calls.append((done, total, result.node_key))
        released.set()
        if broken_callback:
            raise RuntimeError("synthetic UI failure")

    def delay(name):
        if name.endswith("2"):
            assert released.wait(3), "the fast result must be published before waiting for every node"

    if transport == "tcp":
        def measure(node, *_args, **_kwargs):
            delay(node["name"])
            return remote_proxy.ProxyNodeLatencyResult(remote_proxy.proxy_node_key(node), True, 15)
        monkeypatch.setattr(remote_proxy, "measure_proxy_node_latency", measure)
        results = remote_proxy.measure_proxy_node_latencies(nodes, progress_callback=report)
    else:
        @contextmanager
        def session(_binary, node):
            delay(node["name"])
            yield SimpleNamespace(proxy_url="http://127.0.0.1:1234")
        monkeypatch.setattr(local_proxy, "_ensure_local_dirs", lambda: None)
        monkeypatch.setattr(local_proxy, "_ensure_mihomo_binary", lambda: Path("synthetic.exe"))
        monkeypatch.setattr(local_proxy, "_ISOLATED_MIHOMO_SHUTTING_DOWN", threading.Event())
        monkeypatch.setattr(local_proxy, "_isolated_mihomo_session", session)
        monkeypatch.setattr(local_proxy, "_probe_ai_url_through_explicit_http_proxy", lambda *args:
                            local_proxy.LocalAIProxyProbeResult(args[1], True, status=204, elapsed_ms=15))
        results = local_proxy.measure_proxy_node_data_plane_latencies(nodes, progress_callback=report)
    assert len(results) == 2 and all(item.ok for item in results.values())
    assert [call[:2] for call in calls] == [(1, 2), (2, 2)]
    assert {call[2] for call in calls} == set(results)


@pytest.mark.parametrize("changed_context", [False, True])
def test_local_progress_is_visible_before_udp_finishes_but_never_changes_other_subscription(monkeypatch, changed_context):
    nodes = [_node(1, "vless"), _node(2)]
    values = {remote_proxy.proxy_subscription_node_key(node): remote_proxy.ProxyNodeLatencyResult(
        remote_proxy.proxy_subscription_node_key(node), True, 20,
    ) for node in nodes}
    tab = object.__new__(LocalProxyTab)
    tab._busy = False
    tab._subscription_nodes = nodes
    tab._latency_results = {}
    tab._quality_results = {}
    tab._saved_subscription_load_generation = 1
    tab._subscription_batch_nodes = lambda: nodes
    tab._subscription_batch_scope_label = lambda: "全部"
    tab._current_subscription_profile_id = lambda: "synthetic-profile"
    tab._selected_subscription_node_key = lambda: "original"
    tab._set_busy = lambda value: setattr(tab, "_busy", value)
    tab._set_status = lambda *_args: None
    rendered = []
    tab._set_subscription_nodes = lambda *args, **kwargs: rendered.append(dict(tab._latency_results))
    tab._run_on_ui_thread = lambda callback: callback()
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: None
    monkeypatch.setattr("ui.tabs.local_proxy_tab.threading.Thread", _ImmediateThread)
    monkeypatch.setattr("ui.tabs.local_proxy_tab.show_toast", lambda *args, **kwargs: None)
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_latencies", lambda *args, **kwargs: None)
    monkeypatch.setattr(local_proxy, "select_stable_local_proxy_node", lambda *args, **kwargs: (None, {}))

    def tcp(items, **kwargs):
        if changed_context:
            tab._saved_subscription_load_generation = 2
        result = values[remote_proxy.proxy_subscription_node_key(items[0])]
        kwargs["progress_callback"](1, 1, result)
        return {result.node_key: result}

    def udp(items, **kwargs):
        assert tab._busy
        if changed_context:
            assert not rendered
        else:
            assert len(rendered) == 1 and len(rendered[0]) == 1
        result = values[remote_proxy.proxy_subscription_node_key(items[0])]
        kwargs["progress_callback"](1, 1, result)
        return {result.node_key: result}

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", tcp)
    monkeypatch.setattr(local_proxy, "measure_proxy_node_data_plane_latencies", udp)
    tab._verify_subscription_stability()
    assert not tab._busy
    assert (not rendered) if changed_context else (rendered[-1] == values)
