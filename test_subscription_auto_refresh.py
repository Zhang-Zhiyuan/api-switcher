"""Saved subscription timers never depend on foreground editors or live hosts."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from core import local_proxy, proxy_routing, remote_proxy, subscription_auto_refresh as worker
from ui import subscription_auto_refresh as runner
from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab


@pytest.fixture(params=["local", "ssh"])
def saved_scope(request, monkeypatch):
    state = {"active_profile_id": "a", "profiles": {
        "a": {"id": "a", "name": "Home", "url": "https://a.example/sub"},
        "b": {"id": "b", "name": "DC", "url": "https://b.example/sub"},
        "unused": {"id": "unused", "url": "https://unused.example/sub"},
    }}
    routes = {"service_profile_bindings": {"claude": "a", "youtube": "b"}}
    events = []
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: state)
    monkeypatch.setattr(local_proxy, "_load_local_proxy_routing_preferences_strict", lambda: routes)
    monkeypatch.setattr(proxy_routing, "load_ssh_routes", lambda name: routes)
    monkeypatch.setattr(proxy_routing, "host_lock", lambda name: nullcontext())
    monkeypatch.setattr(worker, "_cached_node_keys", lambda profile: {"original"})
    monkeypatch.setattr(local_proxy, "local_proxy_subscription_direct_fallback_allowed", lambda: False)

    def fetch(url, **kwargs):
        events.append(("fetch", kwargs["profile_id"], url, kwargs))
        return SimpleNamespace(nodes=(kwargs["profile_id"],))

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fetch)
    monkeypatch.setattr(local_proxy, "refresh_running_local_service_routes_from_subscription",
                        lambda nodes, **kwargs: events.append(("routes", kwargs["profile_id"])) or "routes updated")
    monkeypatch.setattr(remote_proxy, "refresh_running_ai_proxy_from_subscription",
                        lambda name, nodes, **kwargs: events.append(("routes", kwargs["profile_id"], name, kwargs)) or "routes updated")
    monkeypatch.setattr(local_proxy, "current_local_ai_proxy_node_key", lambda: "original")
    monkeypatch.setattr(remote_proxy, "_read_remote_managed_proxy_node", lambda *args: {"key": "original"})
    monkeypatch.setattr(remote_proxy, "proxy_node_key", lambda node: node["key"])
    monkeypatch.setattr(local_proxy, "refresh_running_local_ai_proxy_from_subscription",
                        lambda nodes, **kwargs: events.append(("main", kwargs["profile_id"])) or "main updated")
    return SimpleNamespace(scope=request.param, state=state, routes=routes, events=events,
                           run=lambda: worker.refresh_saved_subscriptions(request.param, server_names=("confirmed",)))


def test_all_bound_subscriptions_download_before_single_apply_without_activation(saved_scope):
    result = saved_scope.run()
    assert [event[0] for event in saved_scope.events] == ["fetch", "fetch", "routes"]
    assert set(result["results"]) == {"a", "b"}
    assert result["errors"] == []
    for event in saved_scope.events[:2]:
        assert event[3]["activate"] is False
        assert event[3]["allow_direct_fallback"] is False
        assert event[3]["recovery_proxy_provider"] is local_proxy.local_proxy_subscription_recovery_session
    if saved_scope.scope == "ssh":
        assert saved_scope.events[-1][2] == "confirmed"
        assert saved_scope.events[-1][3]["persist_selection"] is False
        assert "strict_privacy" not in saved_scope.events[-1][3]


def test_one_download_failure_does_not_skip_other_bound_subscription(saved_scope, monkeypatch):
    fetch = remote_proxy.fetch_proxy_subscription

    def fail_one(url, **kwargs):
        if kwargs["profile_id"] == "a":
            raise OSError("synthetic timeout")
        return fetch(url, **kwargs)

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", fail_one)
    result = saved_scope.run()
    assert set(result["results"]) == {"b"}
    assert any("保留已有缓存" in error for error in result["errors"])
    assert saved_scope.events[-1][:2] == ("routes", "b")


def test_removed_bindings_cannot_fall_through_to_changing_default(saved_scope, monkeypatch):
    fetch = remote_proxy.fetch_proxy_subscription

    def unbind_during_download(url, **kwargs):
        result = fetch(url, **kwargs)
        saved_scope.routes["service_profile_bindings"] = {}
        return result

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", unbind_during_download)
    saved_scope.run()
    assert all(event[0] == "fetch" for event in saved_scope.events)


def test_unknown_default_ownership_only_updates_cache(saved_scope, monkeypatch):
    saved_scope.routes["service_profile_bindings"] = {}
    monkeypatch.setattr(worker, "_cached_node_keys", lambda profile: set())
    saved_scope.run()
    assert [event[0] for event in saved_scope.events] == ["fetch"]


def test_source_changed_while_other_subscription_downloads_is_not_overwritten(saved_scope, monkeypatch):
    fetch = remote_proxy.fetch_proxy_subscription

    def change_next_source(url, **kwargs):
        result = fetch(url, **kwargs)
        saved_scope.state["profiles"]["b"]["url"] = "https://changed.example/sub"
        return result

    monkeypatch.setattr(remote_proxy, "fetch_proxy_subscription", change_next_source)
    result = saved_scope.run()
    assert set(result["results"]) == {"a"}
    assert any("来源" in error for error in result["errors"])
    assert [event[1] for event in saved_scope.events if event[0] == "fetch"] == ["a"]


def test_background_refresh_rejects_missing_ssh_allowlist_before_io(monkeypatch):
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: pytest.fail("must reject first"))
    with pytest.raises(ValueError, match="SSH"):
        worker.refresh_saved_subscriptions("ssh", server_names=[])


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class InlineThread:
    def __init__(self, *, target, **kwargs):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture(params=["local", "ssh"])
def timer(request, monkeypatch):
    kind = request.param
    tab = object.__new__(LocalProxyTab if kind == "local" else SSHTab)
    prefix = "_" if kind == "local" else "_proxy_"
    tab._destroyed = False
    tab._ssh_busy = False
    tab._proxy_periodic_update_servers = ("confirmed",)
    tab._selected_sync_server_names = lambda: ["current-unconfirmed"]
    setattr(tab, prefix + "busy", False)
    setattr(tab, prefix + "periodic_update_running", False)
    setattr(tab, prefix + "periodic_update_after_id", None)
    setattr(tab, prefix + "periodic_update_var", Value(True))
    setattr(tab, prefix + "periodic_update_entry", None)
    setattr(tab, prefix + "periodic_update_interval_saved", 30)
    calls = SimpleNamespace(after={}, delays=[], status=[], busy=[], release=[], worker=[], refresh=[], nodes=[], dirty=False)

    def after(delay, callback):
        token = "timer-" + str(len(calls.delays))
        calls.delays.append(delay)
        calls.after[token] = callback
        return token

    tab.after = after
    tab.after_cancel = lambda token: calls.after.pop(token, None)

    def set_busy(value):
        setattr(tab, prefix + "busy", value)
        calls.busy.append(value)

    setattr(tab, "_set_busy" if kind == "local" else "_set_proxy_busy", set_busy)
    setattr(tab, "_set_status" if kind == "local" else "_set_proxy_status", lambda *args: calls.status.append(args))
    setattr(tab, "_refresh_subscription_profile_options" if kind == "local" else "_refresh_proxy_subscription_profile_options", lambda **kwargs: calls.refresh.append(kwargs))
    setattr(tab, "_current_subscription_profile_id" if kind == "local" else "_current_proxy_subscription_profile_id", lambda: "a")
    setattr(tab, prefix + "subscription_profile_blocks_automatic_refresh", lambda: calls.dirty)
    setattr(tab, "_selected_subscription_node_key" if kind == "local" else "_selected_proxy_subscription_node_key", lambda: "selected")
    setattr(tab, "_set_subscription_nodes" if kind == "local" else "_set_proxy_subscription_nodes", lambda *args, **kwargs: calls.nodes.append((args, kwargs)))
    tab._request_route_catalog_refresh = lambda: None
    tab._save_subscription_profile = lambda **kwargs: pytest.fail("automatic task must not save editor")
    tab._save_proxy_subscription_profile = tab._save_subscription_profile
    monkeypatch.setattr(remote_proxy, "try_acquire_proxy_subscription_hot_update", lambda: True)
    monkeypatch.setattr(remote_proxy, "release_proxy_subscription_hot_update", lambda: calls.release.append(True))

    def refresh(scope, **kwargs):
        calls.worker.append((scope, kwargs))
        return {"results": {"a": SimpleNamespace(nodes=("new-node",))}, "errors": [], "apply_messages": []}

    monkeypatch.setattr(worker, "refresh_saved_subscriptions", refresh)
    return SimpleNamespace(tab=tab, calls=calls, kind=kind, prefix=prefix,
                           start=lambda factory=InlineThread: runner.start_saved_refresh(tab, scope=kind, thread_factory=factory),
                           schedule=tab._schedule_periodic_update if kind == "local" else tab._schedule_proxy_periodic_update,
                           toggle=tab._on_periodic_update_toggle if kind == "local" else tab._on_proxy_periodic_update_toggle)


def test_timer_schedule_does_not_depend_on_disk_writes(timer, monkeypatch):
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_state", lambda **kwargs: pytest.fail("timer must not write"))
    timer.schedule()
    assert timer.calls.delays == [30 * 60000]
    timer.schedule(retry=True)
    assert timer.calls.delays[-1] == 60000
    assert len(timer.calls.after) == 1


def test_automatic_schedule_preserves_unconfirmed_interval_input(timer):
    class DraftEntry:
        def get(self):
            pytest.fail("background rescheduling must not read or normalize the interval draft")

    setattr(timer.tab, timer.prefix + "periodic_update_entry", DraftEntry())
    timer.schedule()
    assert timer.calls.delays == [30 * 60000]
    timer.schedule(retry=True)
    assert timer.calls.delays[-1] == 60000


def test_replacement_schedule_failure_keeps_existing_timer(timer):
    timer.schedule()
    original = getattr(timer.tab, timer.prefix + "periodic_update_after_id")
    timer.tab.after = lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic after failure"))
    with pytest.raises(RuntimeError, match="synthetic after failure"):
        timer.schedule()
    assert getattr(timer.tab, timer.prefix + "periodic_update_after_id") == original
    assert list(timer.calls.after) == [original]


def test_background_timer_runs_with_saved_targets_and_preserves_dirty_editor(timer):
    timer.calls.dirty = True
    timer.start()
    assert timer.calls.worker == [(timer.kind, {"server_names": () if timer.kind == "local" else ("confirmed",)})]
    assert timer.calls.refresh == [{"preserve_editor": True}]
    assert timer.calls.nodes == []
    assert timer.calls.release == [True]
    assert timer.calls.busy == [True, False]
    assert timer.calls.delays[-1] == 30 * 60000


def test_background_busy_retries_soon_without_starting_worker(timer):
    setattr(timer.tab, timer.prefix + "busy", True)
    timer.start()
    assert timer.calls.worker == []
    assert timer.calls.release == []
    assert timer.calls.delays == [60000]


def test_worker_start_failure_releases_reservation_and_reschedules(timer):
    class BrokenThread(InlineThread):
        def start(self):
            raise RuntimeError("synthetic thread exhaustion")

    timer.start(BrokenThread)
    assert timer.calls.release == [True]
    assert timer.calls.busy == [True, False]
    assert timer.calls.delays[-1] == 60000
    assert getattr(timer.tab, timer.prefix + "periodic_update_running") is False


def test_late_worker_after_thread_start_failure_never_runs(timer):
    late = []

    class LateThread(InlineThread):
        def start(self):
            late.append(self.target)
            raise RuntimeError("synthetic delayed startup failure")

    timer.start(LateThread)
    late.pop()()
    assert timer.calls.worker == []
    assert timer.calls.release == [True]
    assert timer.calls.busy == [True, False]


def test_start_raises_after_completed_worker_has_only_one_finish(timer):
    class MisreportingThread(InlineThread):
        def start(self):
            self.target()
            raise RuntimeError("synthetic late start error")

    timer.start(MisreportingThread)
    assert len(timer.calls.worker) == 1
    assert timer.calls.release == [True]
    assert timer.calls.busy == [True, False]
    assert timer.calls.delays == [30 * 60000]


def test_start_raises_with_running_worker_does_not_release_lock_early(timer, monkeypatch):
    import threading

    entered, finish = threading.Event(), threading.Event()
    threads = []

    def slow_refresh(*args, **kwargs):
        entered.set()
        assert finish.wait(2)
        return {"results": {}, "errors": []}

    class StartedThread(InlineThread):
        def start(self):
            thread = threading.Thread(target=self.target)
            threads.append(thread)
            thread.start()
            assert entered.wait(2)
            raise RuntimeError("synthetic started-but-raised")

    monkeypatch.setattr(worker, "refresh_saved_subscriptions", slow_refresh)
    try:
        timer.start(StartedThread)
        assert timer.calls.release == []
        assert timer.calls.busy == [True]
        finish.set()
        threads[0].join(2)
        assert not threads[0].is_alive()
        timer.calls.after.pop(next(iter(timer.calls.after)))()
        assert timer.calls.release == [True]
        assert timer.calls.busy == [True, False]
    finally:
        finish.set()
        for thread in threads:
            thread.join(2)


def test_worker_exception_is_reported_without_losing_timer(timer, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic state read failure")

    monkeypatch.setattr(worker, "refresh_saved_subscriptions", fail)
    timer.start()
    assert timer.calls.release == [True]
    assert timer.calls.delays[-1] == 60000
    assert "synthetic state read failure" in timer.calls.status[-1][0]


def test_ui_refresh_exception_still_reenables_controls_and_reschedules(timer):
    def fail(**kwargs):
        raise RuntimeError("synthetic UI refresh failure")

    setattr(timer.tab, "_refresh_subscription_profile_options" if timer.kind == "local" else "_refresh_proxy_subscription_profile_options", fail)
    timer.start()
    assert timer.calls.busy[-1] is False
    assert timer.calls.delays[-1] == 60000


def test_main_thread_polls_completion_instead_of_worker_tk_dispatch(timer):
    pending = []

    class DeferredThread(InlineThread):
        def start(self):
            pending.append(self.target)

    timer.tab._run_on_ui_thread = lambda callback: pytest.fail("worker must not call Tk")
    timer.start(DeferredThread)
    assert timer.calls.delays == [150]
    pending.pop()()
    callback = timer.calls.after.pop(next(iter(timer.calls.after)))
    callback()
    assert timer.calls.release == [True]
    assert timer.calls.busy[-1] is False
    assert timer.calls.delays[-1] == 30 * 60000


def test_poll_timer_error_falls_back_to_idle_without_releasing_active_worker(timer):
    pending, idle = [], []

    class DeferredThread(InlineThread):
        def start(self):
            pending.append(self.target)

    after = timer.tab.after
    timer.tab.after = lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic after failure"))
    timer.tab.after_idle = lambda callback: idle.append(callback) or "idle"
    timer.start(DeferredThread)
    assert timer.calls.release == []
    assert timer.calls.busy == [True]
    timer.tab.after = after
    pending.pop()()
    idle.pop()()
    assert timer.calls.release == [True]
    assert timer.calls.busy == [True, False]
    assert timer.calls.delays[-1] == 30 * 60000


def test_toggle_persistence_failure_keeps_requested_session_timer(timer, monkeypatch):
    def fail(**kwargs):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_state", fail)
    timer.toggle()
    assert timer.calls.delays[-1] == 30 * 60000
    assert "仅本次运行生效" in timer.calls.status[-1][0]
    assert timer.tab._subscription_timer_restored is True


def test_restore_timer_is_offline_idempotent_and_respects_user_toggle(timer, monkeypatch):
    monkeypatch.setattr(remote_proxy, "save_proxy_subscription_state", lambda **kwargs: pytest.fail("restore must not write"))
    state = {f"{timer.kind}_periodic_update_enabled": True,
             f"{timer.kind}_periodic_update_interval_minutes": 45,
             "ssh_periodic_update_servers": ["saved-host"]}
    timer.tab._restore_periodic_subscription_timer(state)
    assert timer.calls.delays == [60000]
    assert timer.calls.worker == []
    getattr(timer.tab, timer.prefix + "periodic_update_var").set(False)
    timer.tab._restore_periodic_subscription_timer(state)
    assert not getattr(timer.tab, timer.prefix + "periodic_update_var").get()
    assert timer.calls.delays == [60000]
    if timer.kind == "ssh":
        assert timer.tab._proxy_periodic_update_servers == ("saved-host",)


def test_failed_restore_can_be_retried_instead_of_marked_initialized(timer):
    state = {f"{timer.kind}_periodic_update_enabled": True, "ssh_periodic_update_servers": ["confirmed"]}
    after = timer.tab.after
    timer.tab.after = lambda *args: (_ for _ in ()).throw(RuntimeError("synthetic after failure"))
    with pytest.raises(RuntimeError):
        timer.tab._restore_periodic_subscription_timer(state)
    assert not getattr(timer.tab, "_subscription_timer_restored", False)
    timer.tab.after = after
    timer.tab._restore_periodic_subscription_timer(state)
    assert timer.tab._subscription_timer_restored is True
    assert len(timer.calls.after) == 1


def test_ssh_old_timer_without_whitelist_cannot_follow_new_ui_selection(monkeypatch):
    tab = object.__new__(SSHTab)
    tab._destroyed = False
    tab._proxy_busy = tab._ssh_busy = tab._proxy_periodic_update_running = False
    tab._proxy_periodic_update_servers = ()
    tab._selected_sync_server_names = lambda: ["not-authorized"]
    messages = []
    tab._set_proxy_status = lambda *args: messages.append(args)
    tab._set_proxy_busy = lambda value: None
    tab._cancel_proxy_periodic_update = lambda: None
    tab._schedule_proxy_periodic_update = lambda **kwargs: None
    monkeypatch.setattr(remote_proxy, "try_acquire_proxy_subscription_hot_update", lambda: pytest.fail("no confirmed host"))
    runner.start_saved_refresh(tab, scope="ssh", thread_factory=InlineThread)
    assert "未保存目标" in messages[0][0]
