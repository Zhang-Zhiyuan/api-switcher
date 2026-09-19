"""Subscription workers must not leave either proxy page locked on startup failure."""

from types import SimpleNamespace

import pytest

from ui.tabs.local_proxy_tab import LocalProxyTab
from ui.tabs.ssh_tab import SSHTab


@pytest.fixture(params=["local", "ssh"])
def fetch_task(request, monkeypatch):
    if request.param == "local":
        from ui.tabs import local_proxy_tab as module

        tab = object.__new__(LocalProxyTab)
        prefix = ""
        operation = tab._fetch_subscription
    else:
        from ui.tabs import ssh_tab as module

        tab = object.__new__(SSHTab)
        prefix = "_proxy"
        operation = tab._fetch_proxy_subscription
    harness = SimpleNamespace(
        tab=tab, module=module, prefix=prefix, operation=operation, worker=False,
        tasks=[], callbacks=[], statuses=[], cache=[], toasts=[], calls=[], updates=[],
        current_nodes=("old-node",), latencies={"old-node": "latency"}, qualities={"old-node": "quality"},
    )
    setattr(tab, f"{prefix}_busy", False)
    setattr(tab, f"{prefix}_periodic_update_running", False)
    setattr(tab, f"{prefix}_saved_subscription_load_generation", 0)
    setattr(tab, f"{prefix}_subscription_profile_blocks_automatic_refresh", lambda: False)
    setattr(tab, f"{prefix}_subscription_url_input", lambda: "https://subscription.example.test/config")
    setattr(tab, f"_save{prefix}_subscription_profile", lambda **_kwargs: {"id": "profile"})
    setattr(tab, f"{prefix}_subscription_options", {"old": "old-node"})
    setattr(tab, f"{prefix}_latency_results", harness.latencies)
    setattr(tab, f"{prefix}_quality_results", harness.qualities)
    setattr(tab, f"{prefix}_subscription_picker", SimpleNamespace(selected_item=lambda: "old-node"))
    setattr(tab, f"_select{prefix}_subscription_node_by_key", lambda _key: True)
    setattr(tab, f"_use_selected{prefix}_subscription_node", lambda **_kwargs: None)
    tab.winfo_exists = lambda: True
    tab.winfo_toplevel = lambda: object()
    tab._run_on_ui_thread = harness.callbacks.append
    tab._request_route_catalog_refresh = lambda: None

    def record(target, *args):
        assert not harness.worker, "subscription worker touched a UI field directly"
        target.append(args)

    def busy(value):
        record(harness.updates, value)
        setattr(tab, f"{prefix}_busy", value)

    def nodes(value, **_kwargs):
        assert not harness.worker
        harness.current_nodes = value

    setattr(tab, f"_set{prefix}_busy", busy)
    setattr(tab, f"_set{prefix}_status", lambda *args: record(harness.statuses, *args))
    setattr(tab, f"_set{prefix}_cache_status", lambda *args: record(harness.cache, *args))
    setattr(tab, f"_set{prefix}_subscription_nodes", nodes)
    monkeypatch.setattr(module, "show_toast", lambda _root, message, **_kwargs: record(harness.toasts, message))

    def fetch(url, **kwargs):
        harness.calls.append((url, kwargs))
        return SimpleNamespace(nodes=("new-node",), proxy_warning="")

    monkeypatch.setattr(module, "remote_proxy", SimpleNamespace(
        load_proxy_subscription_state=lambda: {"active_profile_id": "profile", "selected_node_key": "old-node"},
        fetch_proxy_subscription=fetch,
        load_proxy_subscription_latencies=lambda _state: {},
        load_proxy_subscription_qualities=lambda _state: {},
    ))
    monkeypatch.setattr(module, "local_proxy", SimpleNamespace(
        local_proxy_subscription_direct_fallback_allowed=lambda: True,
        local_proxy_subscription_recovery_session=object(),
        local_proxy_service_bindings_for_profile=lambda _profile_id: (),
    ))

    def run_task(target=None):
        harness.worker = True
        try:
            (target or harness.tasks.pop(0))()
        finally:
            harness.worker = False

    def drain():
        while harness.callbacks:
            harness.callbacks.pop(0)()

    harness.run_task = run_task
    harness.drain = drain
    return harness


def _install_thread(monkeypatch, harness, failure=""):
    error = "synthetic thread failure https://user:password@subscription.example.test/config?token=synthetic-secret"

    class Thread:
        def __init__(self, *, target, **_kwargs):
            if failure == "construct":
                raise OSError(error)
            self.target = target

        def start(self):
            if failure == "started":
                harness.run_task(self.target)
            elif failure != "start":
                harness.tasks.append(self.target)
            if failure:
                raise RuntimeError(error)

    monkeypatch.setattr(harness.module.threading, "Thread", Thread)


@pytest.mark.parametrize("failure", ["construct", "start", "queued"])
@pytest.mark.parametrize("auto", [False, True])
def test_subscription_thread_failure_unlocks_page_and_preserves_nodes(fetch_task, monkeypatch, failure, auto):
    harness = fetch_task
    _install_thread(monkeypatch, harness, failure)
    harness.operation(auto=auto, show_message=not auto)
    assert getattr(harness.tab, f"{harness.prefix}_busy") is False
    assert harness.updates == [(True,), (False,)]
    assert "未启动" in harness.cache[-1][0]
    assert harness.statuses[-1][-1] == ("warning" if auto else "error")
    assert bool(harness.toasts) is not auto
    for message in (*harness.statuses[-1][0:1], *(item[0] for item in harness.toasts)):
        assert "synthetic-secret" not in message
        assert "user:password" not in message
    while harness.tasks:
        harness.run_task()
    harness.drain()
    assert not harness.calls
    assert harness.current_nodes == ("old-node",)
    assert getattr(harness.tab, f"{harness.prefix}_latency_results") is harness.latencies
    assert getattr(harness.tab, f"{harness.prefix}_quality_results") is harness.qualities


@pytest.mark.parametrize("failure", ["", "started"])
def test_subscription_worker_owns_normal_or_already_started_completion(fetch_task, monkeypatch, failure):
    harness = fetch_task
    _install_thread(monkeypatch, harness, failure)
    harness.operation(show_message=False)
    assert getattr(harness.tab, f"{harness.prefix}_busy") is True
    if not failure:
        assert not harness.calls
        harness.run_task()
    assert len(harness.calls) == 1
    assert len(harness.callbacks) == 1
    assert harness.updates == [(True,)]
    harness.drain()
    assert getattr(harness.tab, f"{harness.prefix}_busy") is False
    assert harness.updates == [(True,), (False,)]
    assert harness.current_nodes == ("new-node",)
    assert harness.statuses[-1][-1] == "success"
