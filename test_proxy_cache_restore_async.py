"""Cache restoration failures must finish on the UI thread without data loss."""

from types import SimpleNamespace

import pytest

from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab


@pytest.fixture(params=["local", "ssh"])
def cache_restore(request, monkeypatch):
    backend = request.param
    if backend == "local":
        from ui.tabs import local_proxy_tab as module

        tab = object.__new__(LocalProxyTab)
        prefix = ""
        generation_attr = "_saved_subscription_load_generation"
        restore = tab._load_subscription_cache_for_state
        nodes_method = "_set_subscription_nodes"
        select_method = "_select_subscription_node_by_key"
    else:
        from ui.tabs import ssh_tab as module

        tab = object.__new__(SSHTab)
        prefix = "_proxy"
        generation_attr = "_proxy_saved_subscription_load_generation"
        restore = tab._load_proxy_subscription_cache_for_state
        nodes_method = "_set_proxy_subscription_nodes"
        select_method = "_select_proxy_subscription_node_by_key"

    harness = SimpleNamespace(
        backend=backend,
        module=module,
        tab=tab,
        restore=restore,
        generation_attr=generation_attr,
        prefix=prefix,
        worker=False,
        threads=[],
        callbacks=[],
        cache=[],
        status=[],
        startup=[],
        periodic=[],
        loaded=[],
        selected=[],
        used=[],
        reads=[],
        cached=SimpleNamespace(nodes=("restored-node",)),
        new_latencies={"restored-node": "fresh-latency"},
        new_qualities={"restored-node": "fresh-quality"},
        old_latencies={"current-node": "existing-latency"},
        old_qualities={"current-node": "existing-quality"},
        state={
            "saved_path": "synthetic-cache.yaml",
            "url": "https://example.invalid/subscription",
            "selected_node_key": "restored-node",
        },
    )
    tab._destroyed = False
    setattr(tab, generation_attr, 12)
    setattr(tab, f"{prefix}_latency_results", harness.old_latencies)
    setattr(tab, f"{prefix}_quality_results", harness.old_qualities)
    setattr(tab, f"{prefix}_prefer_quality_sort", False)
    tab._proxy_latency_server_count = 2
    tab._proxy_latency_source_signature = (("existing-server", "127.0.0.1", 22, "test"),)
    tab.current_nodes = ("current-node",)
    tab.selected_key = "current-node"
    tab.subscription_draft = "unsaved subscription name / URL"
    tab.connection_draft = "unsaved connection fields"
    tab.winfo_exists = lambda: True
    tab._run_on_ui_thread = harness.callbacks.append

    def ui_record(target, *args):
        assert not harness.worker, "cache worker touched a UI field directly"
        target.append(args)

    def set_nodes(nodes, *, preserve_key):
        ui_record(harness.loaded, tuple(nodes), preserve_key)
        tab.current_nodes = tuple(nodes)

    def select(key):
        ui_record(harness.selected, key)
        tab.selected_key = key

    def use_selected(**kwargs):
        ui_record(harness.used, kwargs)
        tab.connection_draft = "restored connection fields"

    setattr(tab, nodes_method, set_nodes)
    setattr(tab, select_method, select)
    tab._use_selected_proxy_subscription_node = use_selected
    setattr(tab, f"_set{prefix}_cache_status", lambda *args: ui_record(harness.cache, *args))
    setattr(tab, f"_set{prefix}_status", lambda *args: ui_record(harness.status, *args))
    setattr(tab, f"_schedule{prefix}_startup_refresh", lambda: ui_record(harness.startup))
    setattr(
        tab,
        f"_schedule{prefix}_periodic_update",
        lambda **kwargs: ui_record(harness.periodic, kwargs),
    )

    class DeferredThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            harness.threads.append(self.target)

    monkeypatch.setattr(module.threading, "Thread", DeferredThread)

    def read(name, value):
        harness.reads.append(name)
        return value

    # Replace the LazyModule itself: patching its dynamically resolved methods
    # would leave shadow attributes behind and defeat other tests' core mocks.
    monkeypatch.setattr(
        module,
        "remote_proxy",
        SimpleNamespace(
            load_cached_proxy_subscription=lambda _state: read("cache", harness.cached),
            load_proxy_subscription_latencies=lambda _state: read("latencies", harness.new_latencies),
            load_proxy_subscription_qualities=lambda _state: read("qualities", harness.new_qualities),
        ),
    )

    def run_worker():
        harness.worker = True
        try:
            harness.threads.pop(0)()
        finally:
            harness.worker = False

    def drain_callbacks():
        while harness.callbacks:
            harness.callbacks.pop(0)()

    harness.run_worker = run_worker
    harness.drain_callbacks = drain_callbacks
    return harness


def _assert_preserved(harness):
    tab = harness.tab
    assert tab.current_nodes == ("current-node",)
    assert tab.selected_key == "current-node"
    assert tab.subscription_draft == "unsaved subscription name / URL"
    assert tab.connection_draft == "unsaved connection fields"
    assert getattr(tab, f"{harness.prefix}_latency_results") is harness.old_latencies
    assert getattr(tab, f"{harness.prefix}_quality_results") is harness.old_qualities
    assert getattr(tab, f"{harness.prefix}_prefer_quality_sort") is False
    assert tab._proxy_latency_server_count == 2
    assert tab._proxy_latency_source_signature == (("existing-server", "127.0.0.1", 22, "test"),)
    assert not harness.loaded
    assert not harness.selected
    assert not harness.used


def _fail_read(monkeypatch, harness, name="load_cached_proxy_subscription", error=None):
    if error is None:
        error = PermissionError("synthetic file is locked")

    def fail(_state):
        harness.reads.append("failed-read")
        raise error

    monkeypatch.setattr(harness.module.remote_proxy, name, fail)


def test_cache_restore_success_finishes_on_ui_and_schedules_once(cache_restore):
    harness = cache_restore
    harness.restore(harness.state, 12, auto_refresh=True, schedule_periodic=True)
    harness.run_worker()
    _assert_preserved(harness)
    assert len(harness.cache) == len(harness.status) == 1
    assert not harness.startup and not harness.periodic

    callback = harness.callbacks[0]
    harness.drain_callbacks()
    callback()  # A repeated dispatch must not restore/schedule twice.

    assert harness.loaded == [(("restored-node",), "restored-node")]
    assert harness.selected == [("restored-node",)]
    assert harness.cache[-1][-1] == "success"
    assert getattr(harness.tab, f"{harness.prefix}_quality_results") is harness.new_qualities
    if harness.backend == "local":
        assert harness.tab._latency_results is harness.new_latencies
        assert not harness.used
    else:
        assert harness.tab._proxy_latency_results == {}
        assert harness.tab._proxy_latency_server_count == 0
        assert harness.used == [({"show_message": False, "persist_selection": False},)]
    assert harness.startup == [()]
    assert harness.periodic == [({"initial": True},)]


@pytest.mark.parametrize("empty_cache", [None, SimpleNamespace(nodes=())])
def test_empty_cache_keeps_current_nodes_and_refresh_policy(cache_restore, empty_cache):
    harness = cache_restore
    harness.cached = empty_cache
    harness.restore(harness.state, 12, auto_refresh=True, schedule_periodic=True)
    harness.run_worker()
    harness.drain_callbacks()

    _assert_preserved(harness)
    assert harness.reads == ["cache"]
    assert harness.cache[-1][-1] == "warning"
    assert harness.startup == [()]
    assert harness.periodic == [({"initial": True},)]


@pytest.mark.parametrize("error", [PermissionError("synthetic permission denied"), OSError()])
@pytest.mark.parametrize("loader", ["load_cached_proxy_subscription", "load_proxy_subscription_qualities"])
def test_failed_cache_or_metadata_read_reports_error_and_preserves_state(
    cache_restore, monkeypatch, error, loader
):
    harness = cache_restore
    _fail_read(monkeypatch, harness, loader, error)
    harness.restore(harness.state, 12, auto_refresh=True, schedule_periodic=True)
    harness.run_worker()
    assert len(harness.status) == 1
    callback = harness.callbacks[0]
    harness.drain_callbacks()
    callback()

    _assert_preserved(harness)
    assert harness.cache[-1][-1] == "error"
    assert harness.status[-1][-1] == "error"
    assert type(error).__name__ in harness.status[-1][0]
    assert "未保存输入未改动" in harness.status[-1][0]
    assert harness.startup == [()]
    assert harness.periodic == [({"initial": True},)]
    assert not harness.threads  # Failed disk reads do not spawn retry loops.


def test_local_latency_cache_failure_does_not_partially_replace_nodes(cache_restore, monkeypatch):
    harness = cache_restore
    if harness.backend == "ssh":
        # The SSH tab must never restore unattributed Win11 latency results.
        _fail_read(monkeypatch, harness, "load_proxy_subscription_latencies")
        harness.restore(harness.state, 12)
        harness.run_worker()
        harness.drain_callbacks()
        assert harness.cache[-1][-1] == "success"
        assert "failed-read" not in harness.reads
        return

    _fail_read(monkeypatch, harness, "load_proxy_subscription_latencies")
    harness.restore(harness.state, 12, auto_refresh=True, schedule_periodic=True)
    harness.run_worker()
    harness.drain_callbacks()
    _assert_preserved(harness)
    assert harness.cache[-1][-1] == "error"
    assert harness.startup == [()]
    assert harness.periodic == [({"initial": True},)]


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("boundary", ["stale", "destroyed", "missing_widget", "tk_shutdown"])
def test_late_cache_completion_cannot_touch_new_or_destroyed_page(
    cache_restore, monkeypatch, failure, boundary
):
    harness = cache_restore
    if failure:
        _fail_read(monkeypatch, harness)
    harness.restore(harness.state, 12, auto_refresh=True, schedule_periodic=True)
    harness.run_worker()
    if boundary == "stale":
        setattr(harness.tab, harness.generation_attr, 13)
    elif boundary == "destroyed":
        harness.tab._destroyed = True
    elif boundary == "missing_widget":
        harness.tab.winfo_exists = lambda: False
    else:
        def fail_exists():
            raise RuntimeError("synthetic Tcl interpreter has shut down")

        harness.tab.winfo_exists = fail_exists

    harness.drain_callbacks()
    _assert_preserved(harness)
    assert len(harness.cache) == len(harness.status) == 1
    assert not harness.startup and not harness.periodic


@pytest.mark.parametrize("stage", ["construct", "start", "start_after_worker_queued"])
def test_cache_thread_failure_finishes_without_hanging_or_duplicate_updates(
    cache_restore, monkeypatch, stage
):
    harness = cache_restore

    class FailingThread:
        def __init__(self, *, target, **_kwargs):
            if stage == "construct":
                raise OSError("synthetic thread constructor failure")
            self.target = target

        def start(self):
            if stage == "start_after_worker_queued":
                harness.threads.append(self.target)
            raise RuntimeError("synthetic thread start failure")

    monkeypatch.setattr(harness.module.threading, "Thread", FailingThread)
    harness.restore(harness.state, 12, auto_refresh=True, schedule_periodic=True)
    assert harness.status[-1][-1] == "error"
    assert "无法启动缓存恢复任务" in harness.status[-1][0]
    if harness.threads:
        harness.run_worker()
    harness.drain_callbacks()

    _assert_preserved(harness)
    assert len(harness.cache) == len(harness.status) == 2
    assert harness.startup == [()]
    assert harness.periodic == [({"initial": True},)]


@pytest.mark.parametrize(
    ("has_url", "auto_refresh", "schedule_periodic"),
    [(False, True, True), (True, False, True), (True, True, False), (True, False, False)],
)
def test_cache_failure_does_not_enable_unrequested_refresh(
    cache_restore, monkeypatch, has_url, auto_refresh, schedule_periodic
):
    harness = cache_restore
    if not has_url:
        harness.state["url"] = ""
    _fail_read(monkeypatch, harness)
    harness.restore(
        harness.state, 12, auto_refresh=auto_refresh, schedule_periodic=schedule_periodic
    )
    harness.run_worker()
    harness.drain_callbacks()
    _assert_preserved(harness)
    assert len(harness.startup) == int(has_url and auto_refresh)
    assert len(harness.periodic) == int(schedule_periodic)


def test_cache_failure_feedback_redacts_subscription_credentials(cache_restore, monkeypatch):
    harness = cache_restore
    _fail_read(
        monkeypatch,
        harness,
        error=OSError("https://example.invalid/sub?token=synthetic-secret-token"),
    )
    harness.restore(harness.state, 12)
    harness.run_worker()
    harness.drain_callbacks()
    assert "synthetic-secret-token" not in harness.status[-1][0]
    assert "OSError" in harness.status[-1][0]
