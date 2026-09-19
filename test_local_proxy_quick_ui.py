"""Quick UI tests use synthetic nodes and never start a proxy or contact a host."""
import threading
from types import SimpleNamespace

import pytest

from core import local_proxy, remote_proxy
from ui.tabs import local_proxy_tab as module
from ui.widgets.proxy_node_picker import ProxyNodePicker
from ui.theme import COLORS


class InlineThread:
    def __init__(self, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


def node(number, transport="trojan"):
    return remote_proxy.ProxySubscriptionNode(
        index=number,
        node={"name": f"synthetic-{number}", "type": transport,
              "server": "127.0.0.1", "port": 18000 + number, "password": "synthetic"},
    )


def result(item, *, cancelled=False):
    return remote_proxy.ProxyNodeLatencyResult(
        remote_proxy.proxy_subscription_node_key(item), not cancelled,
        latency_ms=None if cancelled else 12, cancelled=cancelled,
    )


@pytest.fixture
def quick_tab(monkeypatch):
    tab = object.__new__(module.LocalProxyTab)
    tab._busy = False
    tab._destroyed = False
    tab._subscription_nodes = [node(1), node(2, "hysteria2"), node(3)]
    tab._latency_results = {"older-profile-result": {"ok": True, "latency_ms": 30}}
    tab._saved_subscription_load_generation = 1
    tab._subscription_batch_nodes = lambda: tab._subscription_nodes[:2]
    tab._subscription_batch_scope_label = lambda: "勾选 2 个节点"
    tab._current_subscription_profile_id = lambda: "synthetic-profile"
    tab._selected_subscription_node_key = lambda: "keep-selection"
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()
    calls = SimpleNamespace(status=[], busy=[], rendered=[], saved=[], queue=[], options=[])
    tab._set_status = lambda *args: calls.status.append(args)

    def busy(value):
        tab._busy = value
        calls.busy.append(value)

    tab._set_busy = busy
    tab._set_subscription_nodes = lambda nodes, **kw: calls.rendered.append((dict(tab._latency_results), kw))
    tab._run_on_ui_thread = calls.queue.append
    tab._quality_cancel_button = SimpleNamespace(configure=lambda **kw: None)

    def forbid(*args, **kwargs):
        pytest.fail("A quick test must not select/apply a node or start deep AI validation")

    tab._select_subscription_node_by_key = forbid
    tab._use_selected_subscription_node = forbid
    monkeypatch.setattr(local_proxy, "select_stable_local_proxy_node", forbid)
    monkeypatch.setattr(module, "show_toast", lambda *args, **kwargs: None)
    # Do not replace threading.Thread globally: the two transport groups use real
    # bounded pool workers, allowing overlap tests without any real network.
    monkeypatch.setattr(module, "threading", SimpleNamespace(
        Thread=InlineThread, Event=threading.Event, Lock=threading.Lock,
    ))
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_latencies",
                        lambda values, **kw: calls.saved.append((dict(values), kw)))

    def measure(items, **kwargs):
        calls.options.append(kwargs)
        values = {}
        for item in items:
            value = result(item)
            values[value.node_key] = value
            kwargs["progress_callback"](len(values), len(items), value)
        return values

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", measure)
    monkeypatch.setattr(local_proxy, "measure_proxy_node_data_plane_latencies", measure)
    return tab, calls


def flush(calls):
    while calls.queue:
        calls.queue.pop(0)()


@pytest.mark.parametrize("all_nodes", [False, True])
def test_quick_covers_scope_preserves_selection_and_never_enters_deep_gate(quick_tab, all_nodes):
    tab, calls = quick_tab
    tab._measure_subscription_latencies(all_nodes=all_nodes)
    assert tab._busy  # UI owns the terminal transition, not a background worker.
    flush(calls)
    scope = tab._subscription_nodes if all_nodes else tab._subscription_nodes[:2]
    expected = {remote_proxy.proxy_subscription_node_key(item) for item in scope}
    assert set(tab._latency_results) == expected | {"older-profile-result"}
    assert calls.saved == [(tab._latency_results, {"profile_id": "synthetic-profile"})]
    assert calls.busy == [True, False]
    assert tab._latency_cancel_event is None
    assert calls.rendered[-1][1] == {"preserve_key": "keep-selection"}
    assert len(calls.options) == 2
    assert all(kw["quick"] and kw["attempts"] == 1 for kw in calls.options)
    assert sorted(kw["max_workers"] for kw in calls.options) == [16, 32]
    assert f"结果 {len(scope)}/{len(scope)}" in calls.status[-1][0]


def test_tcp_and_https_groups_overlap(quick_tab, monkeypatch):
    tab, calls = quick_tab
    barrier = threading.Barrier(2)

    def measure(items, **kwargs):
        barrier.wait(timeout=2)
        return {remote_proxy.proxy_subscription_node_key(item): result(item) for item in items}

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", measure)
    monkeypatch.setattr(local_proxy, "measure_proxy_node_data_plane_latencies", measure)
    tab._measure_subscription_latencies()
    flush(calls)
    assert all(remote_proxy.proxy_node_latency_ok(tab._latency_results[remote_proxy.proxy_subscription_node_key(item)])
               for item in tab._subscription_nodes[:2])


def test_progress_survives_group_exception_and_missing_nodes_have_terminal_results(quick_tab, monkeypatch):
    tab, calls = quick_tab

    def partial(items, **kwargs):
        kwargs["progress_callback"](1, len(items), result(items[0]))
        raise TimeoutError()

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", partial)
    tab._measure_subscription_latencies(all_nodes=True)
    flush(calls)
    assert remote_proxy.proxy_node_latency_ok(tab._latency_results[remote_proxy.proxy_subscription_node_key(tab._subscription_nodes[0])])
    last = tab._latency_results[remote_proxy.proxy_subscription_node_key(tab._subscription_nodes[2])]
    assert not last.ok and "TimeoutError" in last.detail
    assert "可连 2，失败 1，取消 0" in calls.status[-1][0]


def test_cancel_preserves_completed_results_and_does_not_cache_remaining_as_unreachable(quick_tab, monkeypatch):
    tab, calls = quick_tab

    def partial(items, **kwargs):
        first = result(items[0])
        kwargs["progress_callback"](1, len(items), first)
        kwargs["cancel_event"].set()
        return {first.node_key: first}  # UI defensively fills missing cancellation.

    monkeypatch.setattr(remote_proxy, "measure_proxy_node_latencies", partial)
    tab._measure_subscription_latencies(all_nodes=True)
    flush(calls)
    first = tab._latency_results[remote_proxy.proxy_subscription_node_key(tab._subscription_nodes[0])]
    missing = tab._latency_results[remote_proxy.proxy_subscription_node_key(tab._subscription_nodes[2])]
    assert first.ok and missing.cancelled
    assert remote_proxy.proxy_node_latency_label(missing) == "已取消"
    assert not remote_proxy.proxy_node_latency_explicitly_unreachable(missing)
    assert "取消 1" in calls.status[-1][0]
    assert not tab._busy


@pytest.mark.parametrize("change", ["profile", "generation", "destroyed", "replaced"])
def test_stale_completion_does_not_overwrite_new_context(quick_tab, change):
    tab, calls = quick_tab
    tab._measure_subscription_latencies()
    tab._latency_results = {"new-context": {"ok": True}}
    if change == "profile":
        tab._current_subscription_profile_id = lambda: "new-profile"
    elif change == "generation":
        tab._saved_subscription_load_generation += 1
    elif change == "destroyed":
        tab._destroyed = True
    else:
        tab._latency_cancel_event = threading.Event()
    flush(calls)
    assert tab._latency_results == {"new-context": {"ok": True}}
    assert calls.saved[0][1] == {"profile_id": "synthetic-profile"}
    assert not calls.rendered


def test_cache_failure_keeps_results_and_unlocks(quick_tab, monkeypatch):
    tab, calls = quick_tab

    def fail(*args, **kwargs):
        raise OSError("synthetic save failure")

    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_latencies", fail)
    tab._measure_subscription_latencies()
    flush(calls)
    assert len(tab._latency_results) == 3 and not tab._busy
    assert "synthetic save failure" in calls.status[-1][0]
    assert calls.status[-1][1] == "warning"


def test_start_failure_clears_cancel_state_and_busy(quick_tab, monkeypatch):
    tab, calls = quick_tab

    class BrokenThread(InlineThread):
        def start(self):
            raise RuntimeError("synthetic start failure")

    monkeypatch.setattr(module.threading, "Thread", BrokenThread)
    tab._measure_subscription_latencies()
    assert not tab._busy and tab._latency_cancel_event is None
    assert not calls.saved
    assert "synthetic start failure" in calls.status[-1][0]


def test_cancel_button_only_signals_task_without_early_unlock(quick_tab):
    tab, calls = quick_tab
    tab._latency_cancel_event = threading.Event()
    tab._busy = True
    tab._cancel_subscription_test()
    tab._cancel_subscription_test()
    assert tab._latency_cancel_event.is_set() and tab._busy
    assert len(calls.status) == 1


@pytest.mark.parametrize("blocked", ["busy", "empty"])
def test_no_task_when_busy_or_empty(quick_tab, blocked):
    tab, calls = quick_tab
    if blocked == "busy":
        tab._busy = True
    else:
        tab._subscription_nodes = []
    tab._measure_subscription_latencies()
    assert not calls.saved and not calls.options


def test_picker_cancellation_has_own_filter_and_is_not_a_failed_node():
    nodes = [node(1), node(2), node(3), node(4)]
    picker = object.__new__(ProxyNodePicker)
    picker._nodes = nodes
    picker._latency_results = {
        remote_proxy.proxy_subscription_node_key(nodes[0]): result(nodes[0], cancelled=True),
        remote_proxy.proxy_subscription_node_key(nodes[1]): result(nodes[1]),
        remote_proxy.proxy_subscription_node_key(nodes[2]): remote_proxy.ProxyNodeLatencyResult(
            remote_proxy.proxy_subscription_node_key(nodes[2]), False, detail="synthetic failure",
        ),
    }
    picker._quality_results = {}
    picker._node_meta = {}
    picker._metadata_version = 0
    picker._search_text = lambda: ""
    picker._region_combo = None
    picker._quality_combo = None
    picker._build_node_metadata()
    for mode, expected in [("全部", nodes), ("已取消", nodes[:1]), ("可连", nodes[1:2]),
                           ("不可连", nodes[2:3]), ("未测速", nodes[3:])]:
        picker._filter_combo = SimpleNamespace(get=lambda: mode)
        assert picker._filtered_nodes() == expected
    cancelled = picker._latency_results[remote_proxy.proxy_subscription_node_key(nodes[0])]
    assert picker._latency_color(cancelled) == COLORS["muted_soft"]
    assert picker._summary_counts["cancelled"] == 1


def test_resource_cleanup_error_is_visible_even_after_all_results_returned(quick_tab, monkeypatch):
    tab, calls = quick_tab

    def completed_then_cleanup_failure(items, **kwargs):
        for item in items:
            kwargs["progress_callback"](1, len(items), result(item))
        raise RuntimeError("synthetic temporary resource cleanup failure")

    monkeypatch.setattr(local_proxy, "measure_proxy_node_data_plane_latencies", completed_then_cleanup_failure)
    tab._measure_subscription_latencies()
    flush(calls)
    assert "synthetic temporary resource cleanup failure" in calls.status[-1][0]
    assert calls.status[-1][1] == "warning"
    assert "可连 2，失败 0，取消 0" in calls.status[-1][0]


@pytest.mark.parametrize("count,interval", [(12, 0.25), (255, 0.25), (256, 1.0), (1000, 1.0)])
def test_large_batches_throttle_repaints_without_truncating_final_coverage(quick_tab, monkeypatch, count, interval):
    tab, calls = quick_tab
    tab._subscription_nodes = [node(index) for index in range(count)]
    intervals = []
    original = module.CoalescedProgress

    def progress(*args, **kwargs):
        intervals.append(kwargs["interval"])
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "CoalescedProgress", progress)
    tab._measure_subscription_latencies(all_nodes=True)
    flush(calls)
    assert intervals == [interval]
    assert len(tab._latency_results) == count + 1
    assert f"结果 {count}/{count}" in calls.status[-1][0]
    assert not tab._busy
