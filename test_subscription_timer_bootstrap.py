"""Cold-start timer restore is opt-in and performs UI work on the UI thread."""
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from core import remote_proxy
from ui import app as app_module


@pytest.fixture
def harness(monkeypatch):
    callbacks = queue.Queue()
    events = []
    state = {}
    app = SimpleNamespace(
        _exit_requested=False, _subscription_timer_bootstrap_after_id=None,
        _subscription_timer_bootstrap_running=False,
        _tab_specs={"Win11 代理": ("_local_proxy_tab", "local-module", "Local", False),
                    "SSH 服务器": ("_ssh_tab", "ssh-module", "SSH", False)},
    )
    app._run_on_ui_thread = callbacks.put
    app._set_app_status = lambda message: events.append(("status", message))

    def schedule(delay, callback):
        events.append(("schedule", delay, callback))
        return "retry-id"

    def resolve(label, module, name):
        events.append(("resolve", label, threading.get_ident()))
        return name

    def instantiate(label, cls, *, background):
        events.append(("instantiate", label, background, threading.get_ident()))
        return SimpleNamespace(_restore_periodic_subscription_timer=lambda current: events.append(("restore", label, current)))

    app.after = schedule
    app._resolve_tab_class = resolve
    app._instantiate_tab_from_class = instantiate
    app._restore_subscription_timers = lambda: app_module.App._restore_subscription_timers(app)
    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", lambda: dict(state))

    def complete():
        callbacks.get(timeout=3)()

    yield app, state, events, complete
    app._exit_requested = True
    deadline = time.monotonic() + 3
    while app._subscription_timer_bootstrap_running and time.monotonic() < deadline:
        try:
            callbacks.get(timeout=0.05)()
        except queue.Empty:
            pass


@pytest.mark.parametrize("scope", [None, "local", "ssh"])
def test_only_opted_in_timers_start_without_changing_active_page(harness, scope):
    app, state, events, complete = harness
    if scope:
        state[scope + "_periodic_update_enabled"] = True
        state["ssh_periodic_update_servers"] = ["explicit-test-host"]
    main = threading.get_ident()
    app._restore_subscription_timers()
    complete()
    assert not app._subscription_timer_bootstrap_running
    assert not any(item[0] == "schedule" for item in events)
    if scope is None:
        assert not events
    else:
        label = "Win11 代理" if scope == "local" else "SSH 服务器"
        assert events[0][0:2] == ("resolve", label)
        assert events[0][2] != main
        assert events[1] == ("instantiate", label, True, main)
        assert events[2] == ("restore", label, state)


def test_read_error_keeps_retry_and_never_creates_tabs(harness, monkeypatch):
    app, _state, events, complete = harness

    def fail():
        raise OSError("synthetic unavailable storage")

    monkeypatch.setattr(remote_proxy, "load_proxy_subscription_state", fail)
    app._restore_subscription_timers()
    complete()
    assert not any(item[0] == "instantiate" for item in events)
    assert events[-1][0:2] == ("schedule", 30000)
    assert app._subscription_timer_bootstrap_after_id == "retry-id"


def test_empty_thread_start_error_is_not_mistaken_for_success(harness, monkeypatch):
    app, _state, events, _complete = harness

    def fail(**_kwargs):
        raise RuntimeError()

    monkeypatch.setattr(app_module.threading, "Thread", fail)
    app._restore_subscription_timers()
    assert not app._subscription_timer_bootstrap_running
    assert events[-1][0:2] == ("schedule", 30000)


def test_restore_failure_retries_without_staying_busy(harness):
    app, state, events, complete = harness
    state["local_periodic_update_enabled"] = True

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic UI construction failure")

    app._instantiate_tab_from_class = fail
    app._restore_subscription_timers()
    complete()
    assert not app._subscription_timer_bootstrap_running
    assert events[-1][0:2] == ("schedule", 30000)


def test_one_tab_import_failure_does_not_block_the_other_saved_timer(harness):
    app, state, events, complete = harness
    state.update(local_periodic_update_enabled=True, ssh_periodic_update_enabled=True)
    resolve = app._resolve_tab_class

    def fail_local(label, module, name):
        if label == "Win11 代理":
            raise ImportError("synthetic local page unavailable")
        return resolve(label, module, name)

    app._resolve_tab_class = fail_local
    app._restore_subscription_timers()
    complete()
    assert not app._subscription_timer_bootstrap_running
    assert [event[1] for event in events if event[0] == "restore"] == ["SSH 服务器"]
    assert events[-1][0:2] == ("schedule", 30000)


def test_shutdown_before_ui_dispatch_does_not_restore_or_retry(harness):
    app, state, events, complete = harness
    state["ssh_periodic_update_enabled"] = True
    app._restore_subscription_timers()
    app._exit_requested = True
    complete()
    assert not any(item[0] in {"instantiate", "restore", "schedule"} for item in events)


@pytest.mark.parametrize("field", ["_exit_requested", "_subscription_timer_bootstrap_running"])
def test_exit_or_inflight_restore_cannot_start_duplicate_worker(harness, monkeypatch, field):
    app, _state, events, _complete = harness
    setattr(app, field, True)
    monkeypatch.setattr(app_module.threading, "Thread", lambda **_kwargs: pytest.fail("worker must not start"))
    app._restore_subscription_timers()
    assert not events
    app._subscription_timer_bootstrap_running = False
